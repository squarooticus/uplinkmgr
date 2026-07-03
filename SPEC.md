# uplinkmgr — System Specification

**Version:** 1.0-draft  
**Date:** 2026-05-22  
**Status:** Pre-implementation specification

---

## Table of Contents

1. [Overview](#1-overview)
2. [Terminology](#2-terminology)
3. [System Architecture](#3-system-architecture)
4. [Configuration File](#4-configuration-file)
5. [Component Specifications](#5-component-specifications)
   - 5.1 [uplinkmgr-setup](#51-uplinkmgr-setup)
   - 5.2 [dhcpcd Hook: 50-uplinkmgr](#52-dhcpcd-hook-50-uplinkmgr)
   - 5.3 [uplinkmgr Daemon](#53-uplinkmgr-daemon)
6. [Generated File Formats](#6-generated-file-formats)
   - 6.1 [ifupdown Interface Stanzas](#61-ifupdown-interface-stanzas)
   - 6.2 [dhcpcd Configuration Files](#62-dhcpcd-configuration-files)
   - 6.3 [dhcpcd systemd Units](#63-dhcpcd-systemd-units)
   - 6.4 [radvd Configuration Files](#64-radvd-configuration-files)
   - 6.5 [radvd systemd Units](#65-radvd-systemd-units)
   - 6.6 [nftables NAT Reference Fragment](#66-nftables-nat-reference-fragment)
   - 6.7 [Routing Table Registration](#67-routing-table-registration)
   - 6.8 [Uplink Environment Files](#68-uplink-environment-files)
7. [Naming Conventions and Derived Values](#7-naming-conventions-and-derived-values)
8. [IPv4 Routing Behavior](#8-ipv4-routing-behavior)
9. [IPv6 Routing and Delegation Behavior](#9-ipv6-routing-and-delegation-behavior)
10. [Monitoring and State Machine](#10-monitoring-and-state-machine)
11. [Provisioning and Deprovisioning Sequences](#11-provisioning-and-deprovisioning-sequences)
12. [Boot-Time Behavior](#12-boot-time-behavior)
13. [Daemon Lifecycle and Cleanup](#13-daemon-lifecycle-and-cleanup)
14. [Debian Package Structure](#14-debian-package-structure)
15. [Comparison with systemd-networkd](#15-comparison-with-systemd-networkd)
16. [Constraints, Invariants, and Known Limitations](#16-constraints-invariants-and-known-limitations)
17. [Open Verification Items](#17-open-verification-items)

---

## 1. Overview

`uplinkmgr` is a system for configuring a Debian 13 (Trixie) Linux router to handle multiple internet (WAN) uplinks with automatic failover, for both IPv4 and IPv6.

The design goal is to provide the following simultaneously:

- **Resilient IPv4 connectivity** via metric-ordered default routes in the shared `uplinkmgr` routing table, selected by a global policy rule. The highest-priority live uplink is always used; if it fails, its route is removed and the next uplink's route takes over automatically.
- **Multi-homed IPv6 connectivity** via SLAAC. Internal clients receive prefix advertisements from each live uplink through a dedicated macvlan interface and can use any live uplink's source address for outbound traffic, with policy routing ensuring return traffic uses the correct uplink.
- **Graceful degradation** via radvd AdvDefaultPreference signalling and prefix deprecation when an uplink fails, guiding compliant clients away from failed uplinks without cutting off connectivity immediately.
- **Boot-time connectivity without the daemon** so that Debian's network-wait timeout is never triggered.

### Scope

- **In scope:** DHCP WAN uplinks, IPv6 prefix delegation and SLAAC, ifupdown + dhcpcd-based configuration, radvd for RA, per-uplink policy routing tables, monitoring with hysteresis, Debian packaging.
- **Out of scope:** PPPoE, static WAN configurations, DHCPv6 stateful address assignment to clients, firewall and NAT configuration (left to the administrator — uplinkmgr generates a reference nftables fragment but does not apply it), internal DHCP server configuration, ULA prefix advertisement (assumed pre-configured by the administrator in a separate radvd instance).

---

## 2. Terminology

| Term | Definition |
|------|-----------|
| **Uplink** | A single WAN provider, identified by a name and a physical/virtual WAN interface. |
| **Uplink index** | Zero-based sequential position of the uplink in the `uplinks:` list (uplink 0 is highest priority). |
| **Internal interface** | A LAN-side interface (e.g., a VLAN subinterface) serving a logical internal network. |
| **Macvlan interface** | A virtual interface created as a macvlan child of an internal interface, one per (uplink, internal-interface) pair, used to receive and source IPv6 traffic associated with that uplink. |
| **Per-uplink routing table** | A kernel routing table (identified by number and name) that holds the routes for one uplink's traffic, used by policy routing rules. |
| **PD** | IPv6 Prefix Delegation — the DHCPv6 mechanism by which a WAN router assigns a block of IPv6 addresses (typically a /56 or /48) to the customer router for further sub-delegation. |
| **SLA ID** | Site-Level Aggregator identifier — a number used to sub-divide a delegated prefix into per-interface /64 prefixes. |
| **RA** | Router Advertisement — ICMPv6 message sent by radvd to announce prefixes, routes, and default gateway to clients. |
| **SLAAC** | Stateless Address Autoconfiguration — the process by which IPv6 hosts derive addresses from RA-advertised prefixes. |
| **AdvDefaultPreference** | radvd parameter controlling the preference level (high/medium/low) of the default route announced in RAs. |
| **Hook** | The dhcpcd exit hook script `/usr/libexec/dhcpcd-hooks/50-uplinkmgr`, invoked by dhcpcd on every lease event. |
| **uplinkmgr-setup** | The one-shot provisioning tool that generates all configuration files from the YAML config. |
| **uplinkmgr daemon** | The Python monitoring daemon that tracks uplink health and adjusts the main routing table and radvd configs at runtime. |

---

## 3. System Architecture

### 3.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        /etc/uplinkmgr/uplinkmgr.yaml                │
└───────────────┬─────────────────────────────────────────────────┘
                │ read by
                ▼
┌───────────────────────────┐
│       uplinkmgr-setup       │  (one-shot, run at install/reconfig)
│  generates config files   │
└─────────────┬─────────────┘
              │ writes
              ▼
┌──────────────────────────────────────────────────────────────────┐
│  Generated files:                                                │
│   /etc/network/interfaces.d/uplinkmgr.conf        (macvlan stanzas)    │
│   /etc/dhcpcd.conf                           (single dhcpcd cfg)  │
│   /etc/radvd/radvd-uplinkmgr-<name>.conf     (initial radvd cfg)  │
│   /etc/systemd/system/radvd-uplinkmgr-<name>.service              │
│   /etc/uplinkmgr/uplinkmgr-nat.nft.example   (NAT reference frag) │
│   /etc/iproute2/rt_tables.d/uplinkmgr.conf   (table name→number)  │
│   /etc/uplinkmgr/uplinks/<name>.env          (shell env fragment) │
└──────────────────────────────────────────────────────────────────┘

Boot / runtime event flow:
                                                                    
  ifupdown brings up macvlan interfaces                            
       │                                                           
  dhcpcd.service starts (single instance, all uplink interfaces)          
       │                                                           
  dhcpcd obtains IPv4 lease → invokes exit hook                   
       │  50-uplinkmgr: writes /run/uplinkmgr/<name>.ipv4.state,     
       │              signals daemon (SIGUSR1)                     
       │                                                           
  dhcpcd obtains IPv6 PD → invokes exit hook                      
       │  50-uplinkmgr: writes PD + IA_NA state files,             
       │              signals daemon (SIGUSR1)                     
       │                                                           
  radvd-uplinkmgr-<name>.service starts radvd for that uplink       
       │  (radvd advertises prefixes and default route via         
       │   the macvlan interfaces)                                 
       │                                                           
  uplinkmgr daemon runs continuously                                 
       │  monitors uplinks (ping IPv4/IPv6)                        
       │  on SIGUSR1: reconciles routes and ip rules from state    
       │              files; rate-limited radvd restart            
       │  on IPv4 health change: reconciles IPv4 route             
       │  on IPv6 health change: SIGHUPs radvd (pref change)      
       ▼
```

### 3.2 Responsibility Matrix

| Responsibility | uplinkmgr-setup | dhcpcd hook | uplinkmgr daemon |
|---------------|:-------------:|:-----------:|:--------------:|
| Create macvlan interface definitions | ✓ | | |
| Generate dhcpcd configs | ✓ | | |
| Generate systemd service units | ✓ | | |
| Generate initial radvd configs | ✓ | | |
| Generate NAT reference fragment | ✓ | | |
| Register routing table names/numbers | ✓ | | |
| Write per-uplink env files | ✓ | | |
| Write IPv4 gateway state file | | ✓ | |
| Write IPv6 gateway state file | | ✓ | |
| Write IPv6 PD state file | | ✓ | |
| Write IPv6 IA_NA state file | | ✓ | |
| Signal daemon (SIGUSR1) | | ✓ | |
| Remove state files on EXPIRE/STOP | | ✓ | |
| Install IPv4 policy rules (suppress + uplinkmgr lookup) | | | ✓ |
| Add/remove routes in shared IPv4 uplinkmgr table | | | ✓ |
| Add/remove IPv6 routes in per-uplink tables | | | ✓ |
| Install/remove ip -6 rules | | | ✓ |
| Monitor uplink health | | | ✓ |
| Regenerate radvd configs at runtime | | | ✓ |
| SIGHUP radvd instances | | | ✓ |
| Cleanup on daemon stop | | | ✓ |

### 3.3 Interfaces Added Per Uplink

For a system with uplinks `[comcast (idx 0), starlink (idx 1)]` and internal interfaces `[vlan10, vlan20]`, the following macvlan interfaces are created:

| Macvlan interface | Parent | Uplink | Internal iface |
|------------------|--------|--------|----------------|
| `vlan10-u0` | vlan10 | comcast (0) | vlan10 |
| `vlan20-u0` | vlan20 | comcast (0) | vlan20 |
| `vlan10-u1` | vlan10 | starlink (1) | vlan10 |
| `vlan20-u1` | vlan20 | starlink (1) | vlan20 |

Each macvlan interface gets:
- A deterministic MAC address (see §7)
- A deterministic link-local IPv6 address (see §7)
- An IPv6 global address from the delegated /64 for that (uplink, interface) pair (assigned by the dhcpcd hook)

---

## 4. Configuration File

### 4.1 Location

`/etc/uplinkmgr/uplinkmgr.yaml`

This file is installed as an example/default at package install time and is **not overwritten on upgrade** (managed via `dpkg-divert` or by checking existence in the `postinst` script).

### 4.2 Full Schema

```yaml
uplinkmgr:
  routing_table_start: 160      # first per-uplink routing table number (integer)
  rule_priority_start: 29000    # first ip -6 rule priority (integer)
  reject_wrong_pd_src: false    # prohibit macvlan traffic whose source is from a different uplink's PD prefix (bool)
  exclusive_preferred_pd: false # withdraw all but the highest-priority UP uplink as a default router (AdvPreferredLifetime 0 + AdvDefaultLifetime 0) (bool)
  radvd_min_restart_interval: 60 # minimum seconds between radvd restarts on SIGUSR1 (integer, default: 60)

  monitor:
    interval: 10                # seconds between probe cycles (integer, default: 10)
    failure_threshold: 3        # consecutive failures before deprovisioning (integer, default: 3)
    recovery_threshold: 3       # consecutive successes before reprovisioning (integer, default: 3)
    v4_hosts:                   # list of IPv4 addresses to probe
      - 8.8.8.8
      - 1.1.1.1
    v6_hosts:                   # list of IPv6 addresses to probe
      - 2001:4860:4860::8888
      - 2606:4700:4700::1111
    ping_count: 3               # sequential ping -c 1 attempts per host; succeed if any pass (integer, default: 3)

  networks:                     # internal LAN interfaces, in config-file order
    - name: home                # logical name (used in log messages and comments only)
      interface: vlan10         # kernel interface name
    - name: iot
      interface: vlan20

  uplinks:                      # in priority order; index 0 = highest priority
    - name: comcast             # short identifier (used in filenames, table names)
      interface: eth0           # WAN physical/virtual interface
      ipv6_pd: true             # request IPv6 PD; if false, IPv6 monitoring is skipped
      ipv6_pd_hint: 56          # requested PD prefix length hint (integer, default: 56); ISP may ignore
      metric: 100               # optional; default = 100 * (uplink_index + 1)
    - name: starlink
      interface: eth1
      ipv6_pd: false            # IPv4-only uplink; no macvlan created, no PD requested
```

### 4.3 Field Constraints

- `routing_table_start`: Must be in the range 1–252 (default: 160). Avoid the well-known reserved IDs: 0=unspec, 253=default, 254=main, 255=local. Values above 255 are also valid kernel table numbers, but the default keeps table IDs in the single-byte range for readability in `ip route show table all` output. The range `[routing_table_start, routing_table_start + len(uplinks)]` (1 IPv4 table + one per uplink for IPv6) must not overlap with any table numbers already in `/etc/iproute2/rt_tables` or `/etc/iproute2/rt_tables.d/`.
- `rule_priority_start`: Must leave room for all policy rules (see §7.5). Let `N = len(uplinks) * len(networks)`. IPv4 rules: `len(uplinks) + 2` (suppress + lo_to_uplink per uplink + fwd_to_wan). IPv6 rules: `1 + N + len(uplinks)` without `reject_wrong_pd_src`, `1 + 2*N + len(uplinks)` with it. Total with `reject_wrong_pd_src` on: `3 + 2*len(uplinks) + 2*N`. The configured range `[rule_priority_start, rule_priority_start + 99]` must not overlap any existing rules.
- `uplink.name`: Must consist only of alphanumeric characters and hyphens; must be unique across all uplinks.
- `network.interface` and `uplink.interface`: Must be valid Linux interface names (max 15 chars). They are **not** validated against live interface existence by `uplinkmgr-setup` (the system may be configured before interfaces exist).
- `uplink.metric`: If specified, must be a positive integer. If omitted, defaults to `100 * (uplink_index + 1)`.
- `ipv6_pd: false` uplinks: No macvlan interfaces are created for these uplinks; no radvd config or service is generated; no IPv6 monitoring is performed.

### 4.4 Minimal Working Example

```yaml
uplinkmgr:
  monitor:
    v4_hosts:
      - 8.8.8.8
  networks:
    - name: home
      interface: vlan10
  uplinks:
    - name: primary
      interface: eth0
      ipv6_pd: true
```

All other fields take their defaults.

---

## 5. Component Specifications

### 5.1 `uplinkmgr-setup`

#### 5.1.1 Purpose

`uplinkmgr-setup` is a one-shot idempotent tool that reads the YAML config and (re)generates all static configuration files. It is run:
- At initial package installation (from `postinst`)
- Whenever the admin runs `dpkg-reconfigure uplinkmgr`
- Manually when the config file is changed

It does **not** apply runtime changes (restart services, reload nftables, etc.). That is the administrator's responsibility after running the tool, or it is handled by the package scripts.

**After re-running `uplinkmgr-setup`**, the administrator must restart affected services to pick up the new generated files:
- `systemctl restart dhcpcd` if the dhcpcd config changed (dhcpcd does not reload config on SIGHUP)
- `systemctl restart radvd-uplinkmgr-<name>` or `systemctl kill --signal=SIGHUP radvd-uplinkmgr-<name>` for radvd (SIGHUP is sufficient unless lifetimes need refreshing)
- `systemctl restart uplinkmgr` if the uplink list or monitoring parameters changed

The dhcpcd config is **fairly static** — it only changes when the uplink or network list is structurally modified (uplinks added/removed, networks added/removed, interface names changed, metric or `ipv6_pd_hint` changed). Day-to-day operation does not require re-running setup.

#### 5.1.2 Invocation

```
uplinkmgr-setup [--config /etc/uplinkmgr/uplinkmgr.yaml] [--dry-run]
```

- `--config`: Path to config file. Default: `/etc/uplinkmgr/uplinkmgr.yaml`.
- `--dry-run`: Print what would be written to stdout, write nothing to disk.

#### 5.1.3 Output Files

For each run, `uplinkmgr-setup` writes or overwrites the following files. Existing files are replaced atomically (write to a `.tmp` sibling, then `os.rename()`).

| File | Notes |
|------|-------|
| `/etc/network/interfaces.d/uplinkmgr.conf` | macvlan `iface` stanzas (one per macvlan) |
| `/etc/dhcpcd.conf` | single dhcpcd config covering all uplinks (previous config backed up to `/etc/dhcpcd.conf.pre-uplinkmgr`) |
| `/etc/radvd/radvd-uplinkmgr-<name>.conf` | radvd config (initial/up state), one per IPv6 uplink |
| `/etc/systemd/system/radvd-uplinkmgr-<name>.service` | systemd unit, one per IPv6 uplink |
| `/etc/uplinkmgr/uplinkmgr-nat.nft.example` | NAT reference fragment (not applied automatically) |
| `/etc/iproute2/rt_tables.d/uplinkmgr.conf` | routing table name→number mappings |
| `/etc/uplinkmgr/uplinks/<name>.env` | shell env fragment, one per uplink |

#### 5.1.4 Cleanup of Stale Files

When `uplinkmgr-setup` runs, it removes any files from a previous run whose uplink name no longer exists in the current config. It tracks managed files by scanning for filenames matching the uplinkmgr naming patterns. This prevents stale configs from persisting after an uplink is removed.

**Important:** `uplinkmgr-setup` does **not** disable or stop systemd units — that is left to the package scripts or administrator. It does emit a warning listing any units that should be disabled/stopped.

#### 5.1.5 Directory Creation

`uplinkmgr-setup` creates the following directories if they do not exist:
- `/etc/uplinkmgr/uplinks/`
- `/etc/radvd/` (if radvd is installed)
- `/run/uplinkmgr/` (also created by the systemd service on start via `RuntimeDirectory=`)

#### 5.1.6 Error Handling

`uplinkmgr-setup` exits non-zero and prints a clear error message if:
- The config file is missing or unparseable
- Any uplink name fails the naming constraints
- Routing table numbers would overlap with existing entries in `/etc/iproute2/rt_tables` or `/etc/iproute2/rt_tables.d/` (excluding any file named `uplinkmgr.conf` — that is expected to already contain the old uplinkmgr entries)
- Any interface name exceeds 15 characters
- Any derived macvlan name would exceed 15 characters after truncation logic (see §7.1)

---

### 5.2 dhcpcd Hook: `50-uplinkmgr`

#### 5.2.1 Location

`/usr/libexec/dhcpcd-hooks/50-uplinkmgr`

This is a shell script installed by the Debian package. dhcpcd sources all files in `/usr/libexec/dhcpcd-hooks/` in lexicographic order on every lease event. The `50-` prefix places it after dhcpcd's own built-in hooks (typically `01-test`, `20-resolv.conf`, `30-hostname`).

#### 5.2.2 Hook Environment

When dhcpcd invokes the hook, the following environment variables are set by dhcpcd itself (a non-exhaustive list of those used by the hook):

| Variable | Meaning |
|----------|---------|
| `$reason` | The event reason: `BOUND`, `RENEW`, `REBIND`, `REBOOT`, `EXPIRE`, `RELEASE`, `STOP`, `ROUTERADVERT`, `BOUND6`, `RENEW6`, `EXPIRE6`, `STOP6` |
| `$interface` | The interface dhcpcd is managing for this event |
| `$ip_address` | Assigned IPv4 address (for BOUND/RENEW) |
| `$subnet_mask` | IPv4 subnet mask |
| `$routers` | Space-separated IPv4 default gateway(s) |
| `$new_ip6_address` | Assigned IPv6 address (for BOUND6/RENEW6) |
| `$new_ip6_prefix` | Delegated IPv6 prefix (e.g., `2001:db8::/56`) for PD events |
| `$new_ip6_prefixlen` | Delegated prefix length |

> **Verification item:** Confirm the exact variable names for IPv6 PD events in dhcpcd 10.x. The variables `$new_ip6_prefix` and `$new_ip6_prefixlen` appear in dhcpcd documentation but must be verified against the installed version on Debian 13 (Trixie) (dhcpcd 10.1). See §17.

#### 5.2.3 Identifying the Uplink

The hook identifies which uplink it is running for by sourcing the per-uplink env file:

```sh
UPLINKMGR_ENV_DIR=/etc/uplinkmgr/uplinks
env_file="${UPLINKMGR_ENV_DIR}/${interface}.env"

# If no env file for this interface, this is not an uplinkmgr-managed instance
[ -f "$env_file" ] || return 0
. "$env_file"
```

The `.env` file exports `UPLINKMGR_UPLINK_NAME`, `UPLINKMGR_WAN_IFACE`, and `UPLINKMGR_IPV6_PD` (see §6.8).

**Note:** dhcpcd manages the WAN interface (`eth0`, `eth1`, etc.) **and** all macvlan interfaces for that uplink. Events will fire for each interface. The hook checks `$interface` against `$UPLINKMGR_WAN_IFACE` and returns immediately for macvlan events — all routing and rule management is handled by the daemon, not the hook.

#### 5.2.4 IPv4 BOUND / RENEW / REBIND / REBOOT

Triggered when: `$reason` is one of `BOUND`, `RENEW`, `REBIND`, `REBOOT` and `$interface` matches the WAN interface for this uplink.

Actions:

1. Extract the first gateway from `$routers` as `GW4`.
2. Write the gateway and WAN address to the state file (key=value format):
   ```sh
   mkdir -p /run/uplinkmgr
   printf 'gateway=%s\naddress=%s\n' "$GW4" "${new_ip_address:-}" \
       > "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv4.state"
   ```
   The `address=` field is the WAN IP assigned by dhcpcd (`$new_ip_address`); the daemon uses it for the IPv4 `lo_to_uplink` rule (§7.5).
3. Signal the daemon:
   ```sh
   if [ -f /run/uplinkmgr/uplinkmgr.pid ]; then
       kill -USR1 "$(cat /run/uplinkmgr/uplinkmgr.pid)" 2>/dev/null || true
   fi
   ```

**Note:** dhcpcd adds a default route to the **main** table automatically (this is the route with the configured metric that ensures boot-time connectivity). The hook does **not** add or modify IPv4 routes or policy rules — all routing management is the daemon's responsibility.

#### 5.2.5 IPv4 EXPIRE / RELEASE / STOP

Triggered when: `$reason` is `EXPIRE`, `RELEASE`, or `STOP` and `$interface` matches the WAN interface.

Actions:

1. Remove the state file:
   ```sh
   rm -f "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv4.state"
   ```
2. Signal the daemon:
   ```sh
   if [ -f /run/uplinkmgr/uplinkmgr.pid ]; then
       kill -USR1 "$(cat /run/uplinkmgr/uplinkmgr.pid)" 2>/dev/null || true
   fi
   ```

#### 5.2.6 IPv6 ROUTERADVERT (WAN interface)

Triggered when: `$reason` is `ROUTERADVERT` and `$interface` matches the WAN interface.

This event fires when dhcpcd receives a Router Advertisement on the WAN interface. Confirmed variable names (dhcpcd 10.x): `$nd1_from` (RA source address / gateway), `$nd1_lifetime` (router lifetime in seconds), `$nd1_flags` (RA flag characters: `M` = managed, `O` = other), `$nd1_addr1` (first SLAAC address assigned from the RA prefix), `$nd1_prefix_information1_prefix` (RA prefix address), `$nd1_prefix_information1_length` (RA prefix length).

Actions:

1. Extract gateway, lifetime, prefix, and SLAAC address (if unmanaged):
   ```sh
   GW6="$nd1_from"
   ND1_LIFETIME="${nd1_lifetime:-0}"
   ND1_PREFIX="${nd1_prefix_information1_prefix:-}"
   ND1_PLEN="${nd1_prefix_information1_length:-0}"
   # M flag: managed (DHCPv6 IA_NA); no M flag: SLAAC
   case "$nd1_flags" in
       *M*) SLAAC_ADDR="" ;;
       *)   SLAAC_ADDR="${nd1_addr1:-}" ;;
   esac
   ```

2. Write the IPv6 RA state file (key=value format):
   ```sh
   mkdir -p /run/uplinkmgr
   printf 'gateway=%s\nlifetime=%s\ntimestamp=%s\naddress=%s\nprefix=%s\nplen=%s\n' \
       "$GW6" "$ND1_LIFETIME" "$(date +%s)" "$SLAAC_ADDR" "$ND1_PREFIX" "$ND1_PLEN" \
       > "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6ra.state"
   ```
   `address=` is set only for unmanaged (SLAAC) networks; empty for managed networks. `prefix=` and `plen=` are set for all RAs.

3. Signal the daemon (best-effort; may not be running yet at boot):
   ```sh
   if [ -f /run/uplinkmgr/uplinkmgr.pid ]; then
       kill -USR1 "$(cat /run/uplinkmgr/uplinkmgr.pid)" 2>/dev/null || true
   fi
   ```

**Note:** The hook does **not** install the IPv6 default route — the daemon handles that on SIGUSR1 via its reconcile logic.

#### 5.2.7 IPv6 BOUND6 / RENEW6 (WAN interface — PD assignment)

Triggered when: `$reason` is `BOUND6` or `RENEW6` and `$interface` matches the WAN interface.

This event fires after dhcpcd has received (or renewed) a delegated prefix. The PD variables are present on the **WAN interface** event, not on the macvlan interface events. Confirmed variable names: `$dhcp6_ia_pd1_prefix1` (delegated prefix address), `$dhcp6_ia_pd1_prefix1_length` (prefix length, e.g. 60), `$dhcp6_ia_pd1_prefix1_vltime`, `$dhcp6_ia_pd1_prefix1_pltime`.

Actions:

1. Extract the delegated prefix info. If `$dhcp6_ia_pd1_prefix1` is absent (no PD in this event), exit the handler.
   ```sh
   DELEGATED_PREFIX="$dhcp6_ia_pd1_prefix1"
   DELEGATED_LENGTH="$dhcp6_ia_pd1_prefix1_length"
   VLTIME="$dhcp6_ia_pd1_prefix1_vltime"
   PLTIME="$dhcp6_ia_pd1_prefix1_pltime"
   ```

2. Write the PD state file:
   ```sh
   mkdir -p /run/uplinkmgr
   printf 'delegated_prefix=%s\ndelegated_length=%s\nvltime=%s\npltime=%s\ntimestamp=%s\n' \
       "$DELEGATED_PREFIX" "$DELEGATED_LENGTH" "$VLTIME" "$PLTIME" "$(date +%s)" \
       > "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6pd.state"
   ```
   The daemon derives per-macvlan /64 prefixes from `delegated_prefix`, `delegated_length`, and each network interface's SLA ID (its 0-based index in the `networks:` config list).

3. Write the IA_NA address state file (used by the daemon for the `lo_to_uplink` rule):
   ```sh
   if [ -n "$new_ip6_address" ]; then
       printf 'address=%s\n' "$new_ip6_address" \
           > "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6na.state"
   fi
   ```

4. Signal the daemon (best-effort):
   ```sh
   if [ -f /run/uplinkmgr/uplinkmgr.pid ]; then
       kill -USR1 "$(cat /run/uplinkmgr/uplinkmgr.pid)" 2>/dev/null || true
   fi
   ```

**Note:** The hook does **not** install ip -6 rules — the daemon installs and manages all rules on SIGUSR1 via its reconcile logic.

#### 5.2.8 IPv6 EXPIRE6 / STOP6 (WAN interface)

Triggered when: `$reason` is `EXPIRE6` or `STOP6` and `$interface` matches the WAN interface.

Actions:

1. Remove all IPv6 state files for this uplink:
   ```sh
   rm -f "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6ra.state"
   rm -f "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6pd.state"
   rm -f "/run/uplinkmgr/${UPLINKMGR_UPLINK_NAME}.ipv6na.state"
   ```

2. Signal the daemon:
   ```sh
   if [ -f /run/uplinkmgr/uplinkmgr.pid ]; then
       kill -USR1 "$(cat /run/uplinkmgr/uplinkmgr.pid)" 2>/dev/null || true
   fi
   ```

**Note:** The hook ignores EXPIRE6/STOP6 events on macvlan interfaces — those fire co-temporally with the WAN EXPIRE6 and carry no additional state. All IPv6 routing cleanup (routes, rules) is performed by the daemon on SIGUSR1.

#### 5.2.9 RECONFIGURE (WAN interface — `dhcpcd -g` replay)

Triggered when: `$reason` is `RECONFIGURE` and `$interface` matches the WAN interface. This reason is fired by `dhcpcd -g`/`--reconfigure`, which asks an already-running dhcpcd to re-run `dhcpcd-run-hooks` for every interface's current state without renegotiating anything — used by the daemon at startup (see §5.3.8) to catch up on any hook events it missed while it wasn't running.

The hook internally refactors the per-reason bodies of §5.2.4 (IPv4 BOUND-family), §5.2.6 (ROUTERADVERT), and §5.2.7 (BOUND6-family) into standalone functions (`_handle_ipv4_bound`, `_handle_routeradvert`, `_handle_ipv6_bound`), each already written to no-op safely when its relevant `new_*`/`nd1_*` variables are absent. `RECONFIGURE` calls all three unconditionally, then signals the daemon once:

```sh
_handle_ipv4_bound
_handle_routeradvert
_handle_ipv6_bound reconfigure
_signal_daemon
```

**Exception — IA_NA state is never deleted during RECONFIGURE:** `_handle_ipv6_bound`'s normal (`BOUND6`/`RENEW6`/`REBIND6`) behavior deletes `ipv6na.state` if the IA_NA address variable is absent (§5.2.7) — correct for a real lease event, where "absent" means "no longer assigned." For `RECONFIGURE`, whether dhcpcd populates the IA_NA variable at all is unconfirmed (see §17); treating "absent" as "withdrawn" on an uncertain read-only catch-up pass would risk destroying valid existing state. `_handle_ipv6_bound` takes an optional `reconfigure` argument that skips the deletion branch when set.

#### 5.2.10 Hook Non-uplinkmgr Events

If the hook sources an env file and determines that `$interface` is the WAN interface but `$reason` is an unrecognized value, the hook exits 0 (no-op). The hook must **never** fail with a non-zero exit for unrecognized events — dhcpcd treats hook failures as errors.

---

### 5.3 `uplinkmgr` Daemon

#### 5.3.1 Purpose

The daemon monitors uplink health at regular intervals and manages all kernel routing state:
- **IPv4 global policy rules** (installed at startup): `lookup main suppress_prefixlength 0` and `lookup uplinkmgr`; together these route traffic via the shared uplinkmgr IPv4 table while preserving the main table for local/connected routes.
- **IPv6 global policy rule** (installed at startup): `ip -6 rule add lookup main suppress_prefixlength 0`; covers all IPv6 traffic so internal destinations are routed via the main table before per-uplink rules apply.
- **IPv4 routes** in the shared `uplinkmgr` table: one default route per uplink with metric. Also a default route in each per-uplink table (metric 0) for use by the `lo_to_uplink` rule.
- **IPv4 `lo_to_uplink` rules**: per-uplink `ip rule add from <wan-ip> lookup <per-uplink-table>`; routes router-originated traffic bound to a specific WAN IP via the correct uplink.
- **IPv6 routes** in per-uplink tables: one default route per IPv6 uplink (with expiry); installed on SIGUSR1 when the `ipv6ra.state` file is present, removed when absent.
- **ip -6 rules**: per-macvlan `fwd_to_uplink` (installed once, unconditionally, at startup; static for the daemon's lifetime — see §7.5), per-uplink `lo_to_uplink`, and (optionally) `reject_wrong_pd_src` rules (both of the latter installed/removed as state files appear/disappear).
- **radvd configurations**: updating AdvDefaultPreference and prefix lifetimes based on IPv6 uplink state; restarted on SIGUSR1 (rate-limited) for lifetime refresh, SIGHUPed on health state changes for preference updates.

The hook is responsible only for writing state files and signalling the daemon. All routing and rule management is centralized in the daemon, eliminating race conditions between the hook and daemon.

#### 5.3.2 Invocation

```
uplinkmgr [--config /etc/uplinkmgr/uplinkmgr.yaml] [--state-dir /run/uplinkmgr]
          [--log-level {DEBUG,INFO,WARNING,ERROR}] [--log-clean]
```

The daemon runs in the foreground; systemd handles daemonization.

CLI flags:
- `--config PATH` — config file path (default `/etc/uplinkmgr/uplinkmgr.yaml`).
- `--state-dir DIR` — runtime state directory (default `/run/uplinkmgr`).
- `--log-level {DEBUG,INFO,WARNING,ERROR}` — logging level (default `INFO`).
- `--log-clean` — use a minimal log format: `LEVEL message`, e.g.
  `INFO uplink isp1 transitioned UP -> DOWN`, instead of the default
  `%(asctime)s uplinkmgr %(levelname)s %(message)s`. Intended for running
  under systemd, where journald already timestamps every line and tags it
  with the unit/process identity via its own metadata — the default format's
  timestamp and `uplinkmgr` literal are redundant there and clutter
  `journalctl -u uplinkmgr` output. Defaults off so manual/terminal
  invocations still get a self-contained, readable log line. The packaged
  `uplinkmgr.service` unit (§14.4) passes this flag.

#### 5.3.3 State Directory

`/run/uplinkmgr/` (created as `RuntimeDirectory=uplinkmgr` in the systemd unit, or by `uplinkmgr-setup` if run outside systemd).

The daemon reads and writes:
- `<uplink-name>.ipv4.state` — written by the hook on IPv4 BOUND/RENEW; key=value lines: `gateway` (IPv4 default gateway), `address` (WAN IP assigned by dhcpcd). Present when dhcpcd holds a valid IPv4 lease; absent on EXPIRE/RELEASE/STOP. The daemon reads this to determine the IPv4 gateway for the uplinkmgr table route and the WAN IP for the IPv4 `lo_to_uplink` rule.
- `<uplink-name>.ipv6ra.state` — written by the hook on ROUTERADVERT (WAN interface); key=value lines: `gateway` (`$nd1_from`), `lifetime` (seconds, 0 if infinite), `timestamp` (Unix epoch), `address` (SLAAC address if unmanaged, else empty), `prefix` (RA prefix address), `plen` (RA prefix length). The daemon reads this to install/refresh the per-uplink IPv6 default route and to populate `AdvDefaultLifetime`.
- `<uplink-name>.ipv6pd.state` — written by the hook on WAN BOUND6/RENEW6; key=value lines: `delegated_prefix`, `delegated_length`, `vltime`, `pltime`, `timestamp`. The daemon derives per-macvlan /64 prefixes from `delegated_prefix`/`delegated_length` using each network's SLA ID, installs macvlan ip -6 rules, and uses `vltime`/`pltime` to populate `AdvValidLifetime` and `AdvPreferredLifetime`.
- `<uplink-name>.ipv6na.state` — written by the hook on WAN BOUND6/RENEW6 when `$new_ip6_address` is set; key=value line: `address`. The daemon reads this for managed networks to install the `lo_to_uplink` ip -6 rule (`from <ia-na>/128 iif lo lookup <table>`). For unmanaged (SLAAC) networks the rule uses the RA prefix/plen from `ipv6ra.state` instead.
- `uplinkmgr.pid` — written by the daemon at startup; contains the daemon PID. The hook uses this to send SIGUSR1 when new state arrives.

#### 5.3.4 IPv4 Route Management

The daemon manages IPv4 routing via a **shared `uplinkmgr` routing table** (number: `routing_table_start`) and two global policy rules.

**Global policy rules (installed at daemon startup, removed at shutdown):**
```sh
# IPv4
ip rule add lookup main suppress_prefixlength 0 priority <ipv4_internal_traffic_priority>
ip rule add lookup uplinkmgr priority <ipv4_fwd_to_wan_priority>
# IPv6
ip -6 rule add lookup main suppress_prefixlength 0 priority <ipv6_internal_traffic_priority>
```
The suppress rules cause traffic to route via connected/local routes in the main table (inter-VLAN, local) before the per-uplink rules apply; when only a default route would match, it is suppressed and the packet falls through to the next rule. The `lookup uplinkmgr` rule selects the IPv4 default route by metric. The kernel's `lookup main` rule (priority 32767) is preserved as fallback when the daemon is not running.

**Per-uplink IPv4 `lo_to_uplink` rules** (reconciled from `ipv4.state`):
```sh
ip rule add from <wan_ip> lookup <per_uplink_table> priority <ipv4_lo_to_uplink_priority>
ip route replace default via <GW4> dev <wan_iface> metric 0 table <per_uplink_table>
```
The per-uplink table default route (metric 0, present whenever the shared-table route is present) is what the lo_to_uplink rule resolves to. This ensures router-originated traffic bound to a specific WAN IP exits via the correct uplink rather than the highest-metric uplink.

**Routes in the uplinkmgr table (reconciled on SIGUSR1 and on health state changes):**

The desired IPv4 gateway for an uplink is:
- `gateway` from `<uplink-name>.ipv4.state` if the state file is present **and** the uplink is UP
- `None` (no route) if the state file is absent or the uplink is DOWN

When the desired gateway differs from the installed state:
- If desired is non-None: `ip route replace default via <GW4> dev <wan-iface> metric <metric> table uplinkmgr`
- If desired is None and a route was installed: `ip route del default dev <wan-iface> table uplinkmgr`

`ip route replace` is atomic (kernel-level replace); the daemon uses it unconditionally when a route should be present, avoiding any transitional state. Route deletions log a warning if the command fails (may already be absent).

**Why a separate table (not main):** dhcpcd also writes default routes to the main table with the configured metric. Those routes serve as boot-time fallback and remain managed by dhcpcd. The daemon writes to the separate uplinkmgr table to avoid conflicting with dhcpcd's routes.

#### 5.3.5 radvd Config Regeneration

All radvd config updates use SIGHUP, not `systemctl restart`. This is possible because radvd advertises `min(configured_lifetime, address_lifetime)` for any prefix that has a matching address on the interface. Since dhcpcd sub-delegates a /64 from the PD prefix to each macvlan and keeps that address's preferred/valid lifetimes current, radvd automatically reads the correct remaining lifetimes from the kernel on each RA it sends and on each SIGHUP. The `DecrementLifetimes` internal counter is never needed, so the daemon sets `DecrementLifetimes off` in all generated radvd configs.

| Trigger | Action | Why |
|---------|--------|-----|
| Uplink state change (UP↔DOWN) | SIGHUP | Updates preference tier and lifetime config; radvd re-reads interface address lifetimes |
| SIGUSR1 from hook (new PD or RA data) | SIGHUP (rate-limited) or config write only | Interface address lifetime is already accurate; SIGHUP causes radvd to send fresh RAs immediately |
| SIGUSR2 (admin/unconditional) | SIGHUP | Bypass rate limiting; forces immediate RA with current state |

In all cases the daemon first regenerates the config for **all** IPv6 uplinks (a state change can affect other uplinks' preference tiers), then takes the appropriate action.

**SIGUSR1 rate limiting:** Many ISPs (notably Spectrum) send Router Advertisements at very short intervals (as frequently as once every 2 seconds). Without rate limiting, every RA event from the dhcpcd hook would trigger a SIGHUP. The `radvd_min_restart_interval` config option (default: 60s) controls the minimum time between radvd SIGHUPs triggered by SIGUSR1.

On each SIGUSR1, the daemon checks two conditions:
1. **Elapsed time:** seconds since the last radvd SIGHUP ≥ `radvd_min_restart_interval`
2. **Lifetime urgency:** the minimum remaining upstream RA lifetime across all IPv6 uplinks ≤ `radvd_min_restart_interval` (ensures radvd picks up config changes promptly when the gateway lifetime is short)

If either condition is true, radvd is SIGHUPed and `_last_radvd_sighup` is updated. Otherwise the config files are written (so they stay accurate for the next SIGHUP), but radvd is not signalled, and a debug-level log message records the skip.

Uplinks with `lifetime = 0` in `ipv6ra.state` (infinite router lifetime) are excluded from the lifetime urgency check.

#### 5.3.6 Main Loop

```
while True:
    for each uplink:
        probe IPv4 (if state file exists or uplink was previously UP)
        probe IPv6 (if ipv6_pd=true)
        update state machine
        if state changed: apply provisioning/deprovisioning actions
    sleep(monitor.interval)
```

The probe and state update for each uplink are independent — an uplink's IPv4 and IPv6 states are tracked and acted on separately. A single uplink can be IPv4-UP + IPv6-DOWN simultaneously.

#### 5.3.7 Probing

**IPv4 probe:**
```sh
ping -c 1 -W 2 -n -q -I <wan-iface> <host>
```
Run for each host in `monitor.v4_hosts`. For each host, up to `monitor.ping_count` (default 3) sequential `ping -c 1` attempts are made; the first success short-circuits. The overall probe passes if **any** host/attempt succeeds. `-n` suppresses DNS lookups; `-q` suppresses per-packet output.

The probe is bound to the WAN interface (`-I`); the kernel's `lookup uplinkmgr` rule selects the default route via the uplinkmgr table. If the daemon's policy rules are not yet installed, the probe falls through to the main table's dhcpcd-installed routes.

**IPv6 probe:**
```sh
ping6 -c 1 -W 2 -n -q -I <wan-iface> <host>
```
Run for each host in `monitor.v6_hosts`. Same pass/fail logic and `ping_count` retry semantics.

The IPv6 probe binds to the WAN interface (`-I <wan-iface>`) and the kernel routes the packet via whichever table the `ip -6 rule` chain resolves to: normally the per-uplink table, or the main table via the global `lookup main suppress_prefixlength 0` rule if the per-uplink table has no matching route (§5.3.8 step 5).

**IPv6 probe precondition:** IPv6 probing is performed whenever `ipv6_pd: true` or `ia_na: true` is configured for the uplink — unconditionally, regardless of whether the daemon believes a route is currently installed in the per-uplink table. There is deliberately no route-existence check before probing (unlike an earlier version of this design): the kernel's own rule chain decides what the probe actually reaches, and a failure for any reason (no route in either table, or a genuinely down interface) is a valid DOWN signal. Probing must keep running to be able to observe that signal — including when the per-uplink table's route was silently purged by the kernel itself (e.g. on interface admin-down; see §17). If `ipv6_pd`/`ia_na` are both false for the uplink, IPv6 is never probed and the IPv6 state remains untouched (`ipv6_enabled=False` in `statemachine.update()`).

#### 5.3.8 Startup Behavior

On startup, the daemon:

1. Reads the config file.
2. Initializes all uplink states to `UP` (optimistic start — routes are assumed present).
3. Writes the PID file (`/run/uplinkmgr/uplinkmgr.pid`).
4. If dhcpcd is already running (checked via `/run/dhcpcd/pid`), runs `dhcpcd -g` (reconfigure) and waits for it to complete, asking dhcpcd to replay `dhcpcd-run-hooks` for every interface's current state. Logs a warning and continues on failure or timeout — this is a best-effort catch-up, not a hard startup dependency. Skipped entirely if dhcpcd isn't running yet (see §17): there's nothing to catch up on, and invoking `dhcpcd -g` with no live master to reconfigure risks starting a stray dhcpcd process instead.
5. Installs the global IPv4 policy rules (`lookup main suppress_prefixlength 0` and `lookup uplinkmgr`) and the global IPv6 policy rule (`ip -6 rule add lookup main suppress_prefixlength 0`).
6. Installs the per-macvlan `ipv6_fwd_to_uplink` rule for every `(uplink, network)` pair, unconditionally, with no `from` constraint yet (PD state isn't known at this point in the general case). These rules are static for the rest of the daemon's lifetime — see §7.5.
7. Performs an initial reconcile pass over all state files: installs IPv4 and IPv6 routes and the remaining ip -6 rules (`lo_to_uplink`, optionally `reject_wrong_pd_src`) that correspond to existing state files, and narrows step 6's `fwd_to_uplink` rules' `from` constraint where PD state is already present.
8. Begins the monitoring loop immediately (no delay).

The rationale for optimistic start: at boot, dhcpcd has been running and has configured routes before the daemon starts (see §12). The daemon should not deprovision anything until it has actually observed failures.

The initial reconcile ensures that the daemon's in-memory tracking of installed routes and rules is accurate from startup, so subsequent SIGUSR1 and health-change events only apply the necessary delta.

**Why step 4 is needed:** dhcpcd's hook events are edge-triggered — if a `ROUTERADVERT` or `BOUND6` event fires while uplinkmgr isn't running (e.g. it crashed, was restarted, or dhcpcd simply started first at boot), the corresponding `/run/uplinkmgr/*.state` file is still written correctly (the hook writes state before it signals the daemon), but the `SIGUSR1` notification is lost, and dhcpcd won't re-emit that event just because uplinkmgr later starts — it only fires again when something actually changes (a new RA, a rebind). Without step 4, the daemon's step 7 reconcile pass only picks up whatever was *already* written to disk by the time it runs; anything dhcpcd hasn't gotten around to yet, or already reported while uplinkmgr was down and hasn't had a reason to report again, stays missing until an unrelated future dhcpcd event happens to occur. `dhcpcd -g` closes this gap by forcing an immediate replay of hooks for dhcpcd's *current* state, once the daemon is ready to receive the resulting signals (steps 3 and 4 are ordered so the PID file and signal handlers are already in place before the replay's hook invocations can call `_signal_daemon()`).

---

## 6. Generated File Formats

### 6.1 ifupdown Interface Stanzas

Written to `/etc/network/interfaces.d/uplinkmgr.conf`.

One `iface` stanza per macvlan interface (one per (uplink, internal-interface) pair where `ipv6_pd: true`).

```
# Generated by uplinkmgr-setup. Do not edit by hand.
# Regenerate with: uplinkmgr-setup

auto vlan10-u0
iface vlan10-u0 inet manual
    pre-up ip link add link vlan10 name vlan10-u0 type macvlan mode bridge
    pre-up ip link set vlan10-u0 address 52:00:00:00:00:00
    pre-up sysctl -q net.ipv6.conf.vlan10-u0.addr_gen_mode=1
    up ip link set vlan10-u0 up
    up ip -6 addr add fe80::1:1 dev vlan10-u0 scope link
    down ip -6 addr del fe80::1:1 dev vlan10-u0 scope link 2>/dev/null || true
    down ip link del vlan10-u0 2>/dev/null || true

auto vlan20-u0
iface vlan20-u0 inet manual
    pre-up ip link add link vlan20 name vlan20-u0 type macvlan mode bridge
    pre-up ip link set vlan20-u0 address 52:00:01:00:00:00
    pre-up sysctl -q net.ipv6.conf.vlan20-u0.addr_gen_mode=1
    up ip link set vlan20-u0 up
    up ip -6 addr add fe80::1:1 dev vlan20-u0 scope link
    down ip -6 addr del fe80::1:1 dev vlan20-u0 scope link 2>/dev/null || true
    down ip link del vlan20-u0 2>/dev/null || true

auto vlan10-u1
iface vlan10-u1 inet manual
    pre-up ip link add link vlan10 name vlan10-u1 type macvlan mode bridge
    pre-up ip link set vlan10-u1 address 52:01:00:00:00:00
    pre-up sysctl -q net.ipv6.conf.vlan10-u1.addr_gen_mode=1
    up ip link set vlan10-u1 up
    up ip -6 addr add fe80::1:2 dev vlan10-u1 scope link
    down ip -6 addr del fe80::1:2 dev vlan10-u1 scope link 2>/dev/null || true
    down ip link del vlan10-u1 2>/dev/null || true

auto vlan20-u1
iface vlan20-u1 inet manual
    pre-up ip link add link vlan20 name vlan20-u1 type macvlan mode bridge
    pre-up ip link set vlan20-u1 address 52:01:01:00:00:00
    pre-up sysctl -q net.ipv6.conf.vlan20-u1.addr_gen_mode=1
    up ip link set vlan20-u1 up
    up ip -6 addr add fe80::1:2 dev vlan20-u1 scope link
    down ip -6 addr del fe80::1:2 dev vlan20-u1 scope link 2>/dev/null || true
    down ip link del vlan20-u1 2>/dev/null || true
```

**Notes:**
- `auto` before each `iface` stanza causes ifupdown to bring the interface up at boot (without it, the interface is only activated by explicit `ifup`).
- `addr_gen_mode=1` disables EUI-64 automatic link-local generation so the explicit `fe80::1:<N>` can be assigned without conflict.
- The `pre-up` stanzas run before the interface is brought up; `up` stanzas run after; `down` stanzas run when the interface is taken down.
- Error suppression (`2>/dev/null || true`) on `down` stanzas is intentional: these commands are best-effort cleanup.
- The `macvlan mode bridge` allows the macvlan interface to receive multicast/broadcast from the parent interface, which is necessary for SLAAC (multicast solicited-node addresses) to work.

### 6.2 dhcpcd Configuration File

Written to `/etc/dhcpcd.conf` (a single file for all uplinks). The existing file is backed up to `/etc/dhcpcd.conf.pre-uplinkmgr` before the first write.

Example for two uplinks — `comcast` on `eth0` (IPv6 PD, macvlans `vlan10-u0`/`vlan20-u0`) and `starlink` on `eth1` (IPv4-only):

```
# Generated by uplinkmgr-setup. Do not edit by hand.
# Regenerate with: uplinkmgr-setup

allowinterfaces eth0 vlan10-u0 vlan20-u0 eth1

interface eth0
    metric 100
    ipv6rs
    ia_na 1
    ia_pd 2/::/56 vlan10-u0/0/64 vlan20-u0/1/64
    duid

interface vlan10-u0
    iaid 4096
    ipv6only

interface vlan20-u0
    iaid 4097
    ipv6only

interface eth1
    metric 200

hook /usr/libexec/dhcpcd-hooks/50-uplinkmgr
```

**Notes on dhcpcd config:**
- `allowinterfaces` restricts dhcpcd to the listed WAN and macvlan interfaces, preventing it from managing unrelated interfaces.
- `ipv6rs` enables Router Solicitation on PD-capable WAN interfaces so dhcpcd can discover the provider's IPv6 gateway (triggers `ROUTERADVERT`).
- `ia_na 1`: Requests an IPv6 address via DHCPv6 (IA_NA, IAID=1). Required for the WAN interface to have a routable IPv6 source address.
- `ia_pd 2/::/56 …`: Requests prefix delegation (IAID=2) with a `/56` hint; sub-delegates sequential /64s to each macvlan by SLA ID (0-based, in network config-file order). The ISP may grant a different prefix length than the hint.
- `duid`: Uses a DUID for DHCPv6, ensuring consistent lease and prefix assignment across restarts.
- Each macvlan gets its own `interface <macvlan> { iaid ... }` stanza with an explicit, unique `iaid`. dhcpcd's default IAID is derived from the interface's VLAN ID (or, failing that, the last 4 bytes of its MAC address) — both of which are shared across every uplink's macvlan for the same network interface (they're all children of the same underlying VLAN device), so without an explicit `iaid` these macvlans collide on the same L2 segment. The value is `0x1000 + (uplink_index << 8) + network_index` (`naming.macvlan_iaid()`), guaranteeing uniqueness per (uplink, network) pair.
- `ipv6only`: macvlans exist solely as IPv6-PD delegation targets, so this suppresses dhcpcd's default IPv4 handling on them (DHCP/ARP, and falling back to an IPv4 link-local/APIPA address on failure) — behavior that's pointless and noisy for an interface that only ever carries a delegated IPv6 `/64`. Scoped to macvlan stanzas only; WAN uplink stanzas are untouched since those may legitimately provide IPv4 connectivity.
- `metric`: Sets the metric for the IPv4 default route that dhcpcd adds to the main table.
- `hook`: Explicitly loads the uplinkmgr hook so it runs for all managed interfaces.
- IPv4-only uplinks omit `ipv6rs`, `ia_na`, `ia_pd`, and `duid`; only `metric` is needed.

### 6.3 dhcpcd systemd Units

uplinkmgr uses the `dhcpcd.service` unit supplied by the Debian `dhcpcd` package directly — no custom unit is generated. `uplinkmgr-setup` writes `/etc/dhcpcd.conf` (backing up the previous file to `/etc/dhcpcd.conf.pre-uplinkmgr`), and the standard `dhcpcd.service` is restarted to pick it up.

### 6.4 radvd Configuration Files

Written to `/etc/radvd/radvd-uplinkmgr-<name>.conf`. One file per IPv6 uplink, initially generated in the "all-up" state.

The daemon regenerates these files at runtime (see §11). The format must be identical between `uplinkmgr-setup` and the daemon — they use the same generation logic.

**Lifetime values are sourced from state files, never hardcoded.** The daemon reads `lifetime` (from `ipv6ra.state`), `vltime`, and `pltime` (from `ipv6pd.state`) and computes remaining lifetimes (`max(0, value - elapsed)`). These are written into the config so that when radvd starts (or restarts), it begins counting down from the correct remaining value. State-change transitions (including UP→DOWN) apply via SIGHUP alone — the daemon never issues a full restart for a state change (see §5.3.5); `DecrementLifetimes off` (see below) ensures radvd has no internal counter of its own to preserve across a SIGHUP, so every SIGHUP-triggered config reload takes effect immediately, for `AdvDefaultLifetime` exactly as it already does for `AdvPreferredLifetime`/`AdvValidLifetime`.

**`AdvDefaultLifetime` is clamped before being written.** Per `radvd.conf(5)`, `AdvDefaultLifetime` must be either exactly `0` or between `MaxRtrAdvInterval` and 9000 seconds — and since the remaining-lifetime value decays continuously toward 0 between RA refreshes, it would otherwise routinely land in the forbidden gap between 1 second and the floor. `MaxRtrAdvInterval` is set explicitly to `300` (instead of relying on radvd's own default of 600) on every generated interface block, and the computed lifetime is clamped to `0` or `[300, 9000]` accordingly (`generator._clamp_default_lifetime()`). This clamp is naturally satisfied by the DOWN-state `0` (see below), which needs no separate floor.

Example runtime config for uplink `comcast` (index 0, highest priority, IPv6 UP), with delegated prefix `2001:db8:aaaa::/56`, upstream RA lifetime=1800, vltime=86400, pltime=14400, written 300 seconds after delegation:

```
# Generated by uplinkmgr daemon for uplink: comcast
# Do not edit by hand. This file is regenerated automatically.

interface vlan10-u0
{
    AdvSendAdvert on;
    MaxRtrAdvInterval 300;
    AdvDefaultPreference high;
    AdvDefaultLifetime 1500;        # remaining lifetime: 1800 - 300

    prefix 2001:db8:aaaa::/64
    {
        AdvOnLink on;
        AdvAutonomous on;
        AdvRouterAddr on;
        AdvValidLifetime 86100;     # remaining vltime: 86400 - 300
        AdvPreferredLifetime 14100; # remaining pltime: 14400 - 300
        DecrementLifetimes off;
    };

};

interface vlan20-u0
{
    AdvSendAdvert on;
    MaxRtrAdvInterval 300;
    AdvDefaultPreference high;
    AdvDefaultLifetime 1500;

    prefix 2001:db8:aaaa:0001::/64
    {
        AdvOnLink on;
        AdvAutonomous on;
        AdvRouterAddr on;
        AdvValidLifetime 86100;
        AdvPreferredLifetime 14100;
        DecrementLifetimes off;
    };

};
```

**Initial config (generated by `uplinkmgr-setup` before PD has occurred):** Uses safe placeholder values — `AdvValidLifetime 7200`, `AdvPreferredLifetime 1800`, `AdvDefaultLifetime 1800` — with `DecrementLifetimes off`. The placeholder prefix `::/64` will not match any address on the macvlan (no PD has occurred yet), so radvd does not advertise valid prefix information until the daemon regenerates the config on first PD receipt.

> **Verification item:** Confirm whether radvd on Debian 13 (Trixie) supports the `AdvRouterAddr on` directive to automatically use the macvlan's assigned global address as the router address, and whether `prefix ::/64` with `AdvRouterAddr on` causes it to advertise the delegated /64 automatically when dhcpcd assigns it. If not, the daemon must write the explicit prefix (computed from `<uplink>.ipv6pd.state` using the SLA ID) into the radvd config on each PD assignment. See §17.

**Preference tiers for generated radvd config:**

| Uplink state | AdvDefaultPreference | AdvPreferredLifetime | AdvValidLifetime | AdvDefaultLifetime |
|-------------|---------------------|---------------------|-----------------|--------------------|
| IPv6 UP, primary | `high` | remaining pltime from state file | remaining vltime from state file | remaining lifetime from ipv6ra.state |
| IPv6 UP, non-primary (`exclusive_preferred_pd: false`) | `medium` | remaining pltime from state file | remaining vltime from state file | remaining lifetime from ipv6ra.state |
| IPv6 UP, non-primary (`exclusive_preferred_pd: true`) | `medium` | **0** | remaining vltime from state file | **0** |
| IPv6 DOWN | `low` | 0 | 0 | **0** |

("Remaining" values are `max(0, value - elapsed)` at the time the config is written; for freshly renewed leases this is approximately the full value.)

**`DecrementLifetimes off` is always set** (for UP and DOWN uplinks). radvd advertises `min(configured_lifetime, address_lifetime_on_interface)` — since dhcpcd keeps the macvlan address lifetimes current, radvd tracks remaining lifetimes automatically. The configured values act as upper bounds; the interface address is the actual countdown.

When set to DOWN state:
- `AdvPreferredLifetime 0` — `min(0, anything) = 0`, so radvd immediately signals clients to deprecate addresses from this prefix.
- `AdvValidLifetime 0` — `min(0, anything) = 0`, so radvd immediately signals clients to discard SLAAC addresses derived from this prefix.
- `DecrementLifetimes off` — ensures radvd keeps transmitting RAs with the zero lifetime rather than suppressing the prefix block (which would happen if the counter reached 0 with `on`).
- `AdvDefaultLifetime 0` — withdraws the router from clients' default router list entirely, rather than leaving it advertised as a (now-unreachable) last resort.

### 6.5 radvd systemd Units

Written to `/etc/systemd/system/radvd-uplinkmgr-<name>.service`. One file per IPv6 uplink.

```ini
# Generated by uplinkmgr-setup for uplink: comcast
# Do not edit by hand.

[Unit]
Description=Router advertisement daemon for uplinkmgr uplink: comcast
After=network.target dhcpcd.service
Requires=dhcpcd.service

[Service]
Type=forking
PIDFile=/run/radvd-uplinkmgr-comcast.pid
ExecStartPre=/usr/sbin/radvd --configtest --config /etc/radvd/radvd-uplinkmgr-comcast.conf
ExecStart=/usr/sbin/radvd \
    --config /etc/radvd/radvd-uplinkmgr-comcast.conf \
    --pidfile /run/radvd-uplinkmgr-comcast.pid
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

**Notes:**
- The config-validation command runs as `ExecStartPre`, not a second `ExecStart`: `Type=forking` services permit only one `ExecStart` directive (multiple `ExecStart` lines are only valid for `Type=oneshot`). `ExecStartPre` runs first and, if it fails, prevents `ExecStart` from running at all — so this still guards against starting radvd with a malformed config.
- `--nodaemon` is **not** used with `Type=forking`: the real `ExecStart` process must fork into the background and exit for systemd to consider the service started and track the daemon via `PIDFile`. `--nodaemon` keeps radvd in the foreground, which is incompatible with `Type=forking`.
- `ExecReload` with SIGHUP allows `systemctl reload` or `systemctl kill --signal=SIGHUP` to trigger config re-read.

### 6.6 nftables NAT Reference Fragment

Written to `/etc/uplinkmgr/uplinkmgr-nat.nft.example`.

Firewall and NAT configuration is left to the administrator. `uplinkmgr-setup` generates this reference fragment as a starting point but does **not** apply it automatically.

```nft
# NAT reference fragment generated by uplinkmgr-setup.
# This file is NOT applied automatically.
# Review and incorporate into your nftables configuration as appropriate.
# Regenerate with: uplinkmgr-setup

table ip uplinkmgr_nat {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;

        # Masquerade outbound traffic on all WAN interfaces
        oifname "eth0" masquerade
        oifname "eth1" masquerade
    }
}
```

**Notes:**
- The administrator is responsible for applying these rules to their nftables configuration. The exact method depends on how the administrator has structured their nftables setup.
- The table name `uplinkmgr_nat` is chosen to avoid conflicts with any existing `nat` table the administrator may have configured.
- IPv6 NAT (NPTv6) is **not** included — IPv6 uses native global addresses from the delegated prefix. No masquerade is needed or desired for IPv6.
- `uplinkmgr-setup` regenerates this file with the current set of WAN interfaces whenever the uplinks configuration changes.

### 6.7 Routing Table Registration

Written to `/etc/iproute2/rt_tables.d/uplinkmgr.conf`.

```
# Generated by uplinkmgr-setup.
# Do not edit by hand.

160     uplinkmgr
161     uplinkmgr_comcast
162     uplinkmgr_starlink
```

**Notes:**
- Table `routing_table_start` (default: 160) is the shared IPv4 table named `uplinkmgr`. All uplinks' IPv4 default routes are written here by the daemon; the two IPv4 policy rules point to this table.
- IPv6 per-uplink tables start at `routing_table_start + 1` (default: 161, 162, ...), named `uplinkmgr_<uplink-name>`. Each IPv6-capable uplink gets one table for its per-uplink IPv6 default route and policy routing rules.
- This file is read by iproute2 tools (`ip route`, `ip rule`) to allow table names to be used in commands.
- Files in `/etc/iproute2/rt_tables.d/` are merged with the main `/etc/iproute2/rt_tables` file by iproute2 at runtime. Conflicts (duplicate numbers or names) between this file and the main file or other `.d/` files are an error detected by `uplinkmgr-setup`.

### 6.8 Uplink Environment Files

Written to `/etc/uplinkmgr/uplinks/<uplink-name>.env`. One file per uplink.

These files are sourced by the dhcpcd hook to identify the uplink context.

Example for uplink `comcast` (index 0):

```sh
# Generated by uplinkmgr-setup for uplink: comcast
# Do not edit by hand.
UPLINKMGR_UPLINK_NAME=comcast
UPLINKMGR_WAN_IFACE=eth0
UPLINKMGR_IPV6_PD=true
```

The hook sources the env file via `$interface`: the env file is named `<uplink-name>.env` and a symlink `<wan-iface>.env -> <uplink-name>.env` is created so the hook can load it as `/etc/uplinkmgr/uplinks/${interface}.env`. The hook then checks `$interface == $UPLINKMGR_WAN_IFACE`; if not, it returns immediately (macvlan events are ignored).

**Symlinks created for each uplink** (example with macvlans `vlan10-u0`, `vlan20-u0` for uplink `comcast`):

```
/etc/uplinkmgr/uplinks/comcast.env       (the env file itself)
/etc/uplinkmgr/uplinks/eth0.env       -> comcast.env
/etc/uplinkmgr/uplinks/vlan10-u0.env  -> comcast.env
/etc/uplinkmgr/uplinks/vlan20-u0.env  -> comcast.env
```

The symlinks for macvlan interfaces ensure the hook's early `[ -f "$env_file" ] || return 0` check passes for macvlan events (so the file can be sourced and `$UPLINKMGR_WAN_IFACE` checked), but the hook returns immediately after discovering `$interface != $UPLINKMGR_WAN_IFACE`.

---

## 7. Naming Conventions and Derived Values

### 7.1 Macvlan Interface Names

Pattern: `<internal-iface>-u<uplink-index>`, with any `.` in `<internal-iface>` replaced
by `_`. This is required because a literal `.` in the generated name would be
misread by dhcpcd/ifupdown's `<parent>.<vlan-id>` VLAN-tagged-interface naming
convention (see `interfaces(5)`) — without this substitution, a macvlan derived
from a VLAN parent like `sfp0.20` would itself look like a tagged sub-interface.

Examples:
- Internal interface `vlan10`, uplink index 0 → `vlan10-u0`
- Internal interface `sfp0.20`, uplink index 1 → `sfp0_20-u1`

**Truncation:** Linux interface names are limited to 15 characters (IFNAMSIZ - 1). If the derived name (after dot substitution) exceeds 15 characters, it is truncated as follows:

1. Determine whether the (dot-substituted) interface name ends with a numeric suffix matching `[0-9]+$` (e.g. `20` in `vlan20` or in `sfp0_20`).
2. **If a numeric suffix is present:** preserve it intact and preserve the `-u<N>` uplink suffix intact; truncate only the leading portion (which may include `_` separators) to fit within 15 characters. If the numeric suffix + `-u<N>` together already exceed 15 characters, `uplinkmgr-setup` raises an error and refuses to proceed.
3. **If no numeric suffix is present:** truncate the interface name from the right: `<iface[:15-len(suffix)]><suffix>`.

Examples:
- `abcdefghi1.20`, uplink 1: dot-substituted to `abcdefghi1_20`; numeric suffix `20` (2), uplink suffix `-u1` (3) → 5 chars fixed; 10 available for the leading portion `abcdefghi1_` → truncated to `abcdefghi1` → `abcdefghi120-u1`
- `ethernet-uplink` (no numeric suffix), uplink 3: suffix `-u3` (3); 12 chars for prefix → `ethernet-upli-u3`

`uplinkmgr-setup` validates that no two macvlan names (after truncation) are identical.

### 7.2 MAC Address Assignment

Each macvlan interface `<internal-iface>-u<N>` (for uplink index `N` and internal interface index `M` in config-file order) gets MAC address:

```
52:<N>:<M>:00:00:00
```

- First octet `52` = `0x52` has bits `0b01010010`: bit 1 (LSB of first byte) = 0 (unicast), bit 2 = 1 (locally administered). This is a valid locally administered unicast MAC.
- Second octet encodes the uplink index (0–255).
- Third octet encodes the internal interface index (0–255).
- Remaining octets are zero.

This scheme supports up to 256 uplinks and 256 internal interfaces without collision.

Example:
- uplink 0, iface 0 (`vlan10-u0`): `52:00:00:00:00:00`
- uplink 0, iface 1 (`vlan20-u0`): `52:00:01:00:00:00`
- uplink 1, iface 0 (`vlan10-u1`): `52:01:00:00:00:00`
- uplink 1, iface 1 (`vlan20-u1`): `52:01:01:00:00:00`

### 7.3 Link-Local Address Assignment

All macvlan interfaces for uplink index `N` (regardless of internal interface) get link-local address:

```
fe80::1:<N>
```

- Uplink 0 → `fe80::1:1`
- Uplink 1 → `fe80::1:2`
- Uplink 2 → `fe80::1:3`

The `1:` prefix groups all uplinkmgr-managed router addresses into a recognisable block, distinct from `fe80::1` (the internal interface's own link-local) and from EUI-64-derived addresses. By using distinct link-local addresses, clients that have multiple next-hop candidates can distinguish them at the IPv6 layer, enabling correct source address selection per RFC 6724 rule 5.5.

EUI-64 auto-generation is disabled on each macvlan interface before the link-local is assigned:
```sh
sysctl -q net.ipv6.conf.<iface>.addr_gen_mode=1
```
This must be set before the interface is brought up, which is why it appears in the `pre-up` stanza.

### 7.4 Routing Table Numbers and Names

- **Shared IPv4 table:** number `routing_table_start` (default: 160); name `uplinkmgr`. Contains one IPv4 default route per uplink (with metric). The two global policy rules direct traffic here.
- **Per-uplink IPv6 tables:** numbers `routing_table_start + 1 + uplink_index` (default: 161, 162, ...); names `uplinkmgr_<uplink-name>` (e.g., `uplinkmgr_comcast`, `uplinkmgr_starlink`). Each IPv6-capable uplink gets one table for its IPv6 default route and associated policy routing rules.
- All tables registered in `/etc/iproute2/rt_tables.d/uplinkmgr.conf`
- Total tables used: `1 + len(uplinks)` (1 IPv4 + one per uplink for IPv6)

### 7.5 Rule Priorities

All policy routing rules are installed and removed by the **daemon** (not the hook). Global rules are installed at startup and removed at shutdown; per-uplink/per-macvlan rules are installed/removed as state files appear/disappear.

Let `N = len(uplinks) * len(networks)`, `M = len(networks)`.

**IPv4 rules** (`ip rule` — separate priority namespace from `ip -6 rule`):

- `rule_priority_start + 0` (`ipv4_internal_traffic`): `lookup main suppress_prefixlength 0` — global; installed at startup.
- `rule_priority_start + 1 + uplink_idx` (`ipv4_lo_to_uplink`): `from <wan_ip> lookup <per_uplink_table>` — per uplink; installed when `ipv4.state` is present and uplink is UP.
- `rule_priority_start + 1 + len(uplinks)` (`ipv4_fwd_to_wan`): `lookup uplinkmgr` — global; installed at startup.

**IPv6 rules** (`ip -6 rule` — separate priority namespace from IPv4):

- `rule_priority_start + 0` (`ipv6_internal_traffic`): `lookup main suppress_prefixlength 0` — **single global rule**; installed at startup; replaces the old per-macvlan suppress rules.
- `rule_priority_start + 1 + uplink_idx * M + net_idx` (`ipv6_fwd_to_uplink`): per macvlan; `reject_wrong_pd_src` off: `iif <macvlan> lookup <table>`; on: `from <delegated-prefix>/<len> iif <macvlan> lookup <table>`. **Static for the daemon's lifetime:** installed once, unconditionally, at startup for every `(uplink, network)` pair (`Daemon._setup_ipv6_macvlan_rules()`) — not gated on PD state or uplink health. When `reject_wrong_pd_src` is on, the `from` constraint is updated in place as PD state changes, but the rule itself is never deleted while the daemon runs; if PD is lost, the last-known constraint is left in place rather than reverted (harmless — radvd will have told clients to stop using that prefix, so no legitimate traffic should carry it as a source; see §17). Full removal only happens in `_teardown_all()` (daemon shutdown or config reload). This is deliberately different from `ipv6_lo_to_uplink` and `ipv6_reject_wrong_pd_src` below, which remain driven by live state — see §17 for rationale.
- `rule_priority_start + 1 + N + uplink_idx` (`ipv6_lo_to_uplink`): `from <prefix> iif lo lookup <table>`. For managed networks: `from <ia_na_addr>/128`; for SLAAC networks: `from <ra_prefix>/<ra_plen>` (covers all kernel-assigned addresses from the RA prefix, including privacy addresses). Installed when state is known and uplink is UP.
- `rule_priority_start + 1 + N + len(uplinks) + uplink_idx * M + net_idx` (`ipv6_reject_wrong_pd_src`): `iif <macvlan> prohibit` — only when `reject_wrong_pd_src: true`.

Example with 2 uplinks (`comcast`=0, `starlink`=1), 2 networks, `reject_wrong_pd_src: true`, `rule_priority_start`=29000 (N=4, M=2):

```
# ip rule show (IPv4)
29000:  from all lookup main suppress_prefixlength 0         (ipv4_internal_traffic)
29001:  from <comcast-wan-ip> lookup 161                     (ipv4_lo_to_uplink, comcast)
29002:  from <starlink-wan-ip> lookup 162                    (ipv4_lo_to_uplink, starlink)
29003:  from all lookup 160                                  (ipv4_fwd_to_wan)

# ip -6 rule show (IPv6)
29000:  from all lookup main suppress_prefixlength 0         (ipv6_internal_traffic, global)
29001:  from <comcast-pd>/48 iif vlan10-u0 lookup 161        (ipv6_fwd_to_uplink)
29002:  from <comcast-pd>/48 iif vlan20-u0 lookup 161
29003:  from <starlink-pd>/48 iif vlan10-u1 lookup 162
29004:  from <starlink-pd>/48 iif vlan20-u1 lookup 162
29005:  from <comcast-ia-na>/128 iif lo lookup 161           (ipv6_lo_to_uplink, managed)
29006:  from <starlink-prefix>/64 iif lo lookup 162          (ipv6_lo_to_uplink, SLAAC)
29007:  iif vlan10-u0 prohibit                               (ipv6_reject_wrong_pd_src)
29008:  iif vlan20-u0 prohibit
29009:  iif vlan10-u1 prohibit
29010:  iif vlan20-u1 prohibit
```

**Rule update semantics:** `ip rule del` + `ip rule add` is **not atomic** — a brief window exists where no rule is present. The daemon avoids unnecessary del+add by tracking installed rule parameters and only reinstalling when parameters change. `lo_to_uplink` rules are reinstalled if the uplink address/prefix changes.

### 7.6 SLA IDs for IPv6 PD

When dhcpcd is configured to sub-delegate a received /56 (or other) prefix to macvlan interfaces, each internal interface gets a SLA ID equal to its 0-based index in the `networks:` list:
- networks[0] (`home`/`vlan10`) → SLA ID 0 → first macvlan gets `<prefix>:0::/64`
- networks[1] (`iot`/`vlan20`) → SLA ID 1 → second macvlan gets `<prefix>:1::/64`

This is encoded in the dhcpcd config `ia_pd` directive (see §6.2).

### 7.7 IPv4 Default Route Metrics

Default: `100 * (uplink_index + 1)`
- Uplink 0 (comcast): metric 100
- Uplink 1 (starlink): metric 200

If the `metric` field is specified in the YAML for an uplink, that value overrides the default.

Constraints: Metrics must be unique across all uplinks (validated by `uplinkmgr-setup`). Lower metric = higher priority (the main table's route selection algorithm prefers lower metrics).

---

## 8. IPv4 Routing Behavior

### 8.1 Normal State (all uplinks UP)

The daemon installs two global policy rules and a default route per uplink in the shared `uplinkmgr` table:

```
# ip rule show (relevant entries)
29000:  lookup main suppress_prefixlength 0         # ipv4_internal_traffic
29001:  from 203.0.113.10 lookup 161                # ipv4_lo_to_uplink (comcast wan ip)
29002:  from 198.51.100.50 lookup 162               # ipv4_lo_to_uplink (starlink wan ip)
29003:  lookup uplinkmgr                            # ipv4_fwd_to_wan
32767:  lookup main (kernel default, always present)

# ip route show table uplinkmgr (table 160)
default via 192.168.1.1 dev eth0 metric 100    # comcast
default via 10.0.0.1    dev eth1 metric 200    # starlink

# ip -4 route show table 161 (comcast per-uplink)
default via 192.168.1.1 dev eth0               # for lo_to_uplink use

# ip -4 route show table 162 (starlink per-uplink)
default via 10.0.0.1 dev eth1
```

All IPv4 traffic (from internal interfaces, NATed via nftables masquerade) is directed by the `lookup uplinkmgr` rule and exits through `eth0` (comcast, metric 100). Inter-VLAN and local traffic matches the `suppress_prefixlength 0` rule via the main table's connected routes. Router-originated traffic using comcast's WAN IP is directed by the `lo_to_uplink` rule to table 161 and exits via `eth0`.

dhcpcd also installs default routes in the main table (with the configured metrics) as a fallback. These are not used while the daemon's policy rules are active.

### 8.2 Primary Uplink Fails

The daemon removes the comcast route from the uplinkmgr table:

```
# ip route show table uplinkmgr
default via 10.0.0.1 dev eth1 metric 200    # starlink (now the only route)
```

All IPv4 traffic now exits through `eth1`. No policy rule changes are needed — the same `lookup uplinkmgr` rule now resolves to the starlink route by default.

### 8.3 Routing Tables

**Shared IPv4 table `uplinkmgr` (table `routing_table_start`, default 160):**
Contains one IPv4 default route per uplink when UP. The daemon manages this table; dhcpcd does not write here.

**Per-uplink IPv6 tables** (tables `routing_table_start + 1 + uplink_index`, defaults 161, 162, ...):

Table `uplinkmgr_comcast` (161):
```
default via fe80::1 dev eth0 expires 1800
```

Table `uplinkmgr_starlink` (162):
```
default via fe80::2 dev eth1 expires 1800
```

These are managed by the daemon on SIGUSR1. They are used for:
- IPv6 source-based policy routing (packets sourced from a specific uplink's delegated prefix use that uplink's table)
- IPv6 monitoring precondition check (daemon checks for an IPv6 default route before probing)

### 8.4 NAT

NAT configuration is the administrator's responsibility. `uplinkmgr-setup` generates a reference nftables fragment at `/etc/uplinkmgr/uplinkmgr-nat.nft.example` as a starting point, but does not apply it.

The reference fragment masquerades all outbound traffic on WAN interfaces. Because dead uplinks have their default routes removed from the uplinkmgr table, traffic cannot reach those interfaces; masquerade rules for dead uplinks are never triggered even if left in place.

---

## 9. IPv6 Routing and Delegation Behavior

### 9.1 Prefix Delegation Flow

1. dhcpcd (managing the `comcast` WAN interface `eth0`) sends a DHCPv6 PD request.
2. The ISP assigns a prefix, e.g., `2001:db8:aaaa::/56`.
3. dhcpcd sub-delegates:
   - `2001:db8:aaaa:0000::/64` → `vlan10-u0` (SLA ID 0)
   - `2001:db8:aaaa:0001::/64` → `vlan20-u0` (SLA ID 1)
4. dhcpcd assigns the address `2001:db8:aaaa:0000::1:0/64` to `vlan10-u0` (using `fe80::1:1` as its link-local, the router address is formed from the interface's assigned global prefix + interface identifier from the link-local suffix... see verification item).
5. The dhcpcd hook runs and:
   - Adds `2001:db8:aaaa:0000::/64` to the per-uplink routing table via `vlan10-u0`
   - Adds `2001:db8:aaaa:0001::/64` to the per-uplink routing table via `vlan20-u0`
   - Installs ip -6 rules: `iif vlan10-u0 lookup uplinkmgr_comcast`
6. radvd (comcast instance) reads the prefix from `vlan10-u0` and begins advertising:
   - Prefix `2001:db8:aaaa:0000::/64` on `vlan10-u0` with `AdvDefaultPreference high`
   - Default route via `fe80::1:1` (the macvlan's link-local)

### 9.2 Client Behavior (RFC 6724 Rule 5.5)

A client on `vlan10` receives RAs from:
- `fe80::1` — the router's primary address, advertising ULA prefix (admin-configured)
- `fe80::1:1` — `vlan10-u0`, advertising `2001:db8:aaaa:0000::/64` (comcast)
- `fe80::1:2` — `vlan10-u1`, advertising `2001:db8:bbbb:0000::/64` (starlink, if IPv6-capable)

The client auto-configures multiple global addresses via SLAAC. When sending a packet, it selects the source address per RFC 6724. Rule 5.5 ("Prefer outgoing interface") causes the client to prefer a source address whose "default router" (next-hop) matches the outgoing interface. Since each router has a distinct MAC and link-local, the client can correctly associate each global address with its router and select the correct source address when a specific uplink is desired.

### 9.3 Policy Routing Enforcement

When a packet arrives at `vlan10-u0` (sent by a client to `fe80::1:1`), the ip -6 rule `iif vlan10-u0 lookup uplinkmgr_comcast` directs it to the comcast routing table, ensuring it exits `eth0` regardless of the client's source address (as long as the packet arrived on the correct macvlan).

This prevents a client that sends a packet to `fe80::1:1` (comcast router) from having the packet routed out `eth1` (starlink). It also means a client cannot accidentally use comcast's router as a default gateway for traffic that should use starlink.

### 9.4 Optional Wrong-PD Source Rejection

If `reject_wrong_pd_src: true`, the `fwd_to_uplink` rule is narrowed to `from <delegated-prefix>/<len> iif <macvlan>` (matching only traffic whose source address belongs to that uplink's delegated prefix), and an `ipv6_reject_wrong_pd_src` catch-all rule (`iif <macvlan> prohibit`) is installed at a lower priority. Traffic that arrives on a macvlan with a source address from a different uplink's prefix matches the catch-all and receives an ICMPv6 Destination Unreachable (code 1, "no route") response. This prevents clients from sending traffic to one uplink's router using a source address from a different uplink's prefix delegation.

This is **disabled by default** because it may cause unexpected failures with misconfigured clients and is conservative to enable.

**Connectivity loss on dual-uplink failure:** When `reject_wrong_pd_src: true`, the `ipv6_reject_wrong_pd_src` rules remain installed as long as the uplink's `ipv6pd.state` file is present — they are not removed when the daemon declares an uplink DOWN. If uplinkmgr incorrectly marks both uplinks DOWN (e.g., false-positive probe failures, or the daemon itself stops), traffic from internal clients whose source addresses belong to either uplink's delegated prefix will be prohibited at the macvlan, rather than falling through to dhcpcd's default routes in the main table. This eliminates the best-effort fallback that `reject_wrong_pd_src: false` provides. Administrators who rely on partial connectivity during uplink failures or uplinkmgr restarts should leave this option disabled.

### 9.5 Prefix Subdivision Invariant

The SLA ID assignment is **fixed at config-file order** — it does not change if uplinks are added or removed. If the config changes (e.g., a new network is added), `uplinkmgr-setup` must be re-run and dhcpcd restarted to re-request PD with updated SLA IDs.

### 9.6 Optional Exclusive Preferred-Lifetime Enforcement

If `exclusive_preferred_pd: true`, only the highest-priority working IPv6 uplink (the first UP uplink by config-file index) is visible to clients as a default router at all. All other UP ("secondary") uplinks advertise `AdvPreferredLifetime 0` (immediately deprecating their SLAAC addresses) **and** `AdvDefaultLifetime 0` (fully withdrawing them from clients' default router list), not merely a lower `AdvDefaultPreference`. `AdvValidLifetime` is unchanged for secondary UP uplinks — existing connections using deprecated addresses continue until the valid lifetime expires.

**Use case:** the purpose of this option is to ensure only one uplink is ever visible to clients at all, because RFC 6724 rule 5.5 ("prefer source address matching outgoing interface") is not implemented in most stacks yet. Clients that ignore it, or have other broken source/router selection, may pick a default router or source address from the wrong uplink's prefix for a given destination. The resulting traffic is asymmetric — it arrives on one macvlan but the client's destination is via a different one — and can be blackholed or refused (especially with `reject_wrong_pd_src: true`). Merely lowering a secondary uplink's preference isn't sufficient, since a client that never implemented rule 5.5 may still use it as a default router; `exclusive_preferred_pd: true` removes the ambiguity entirely by ensuring at any given time only one uplink is a default router or has preferred SLAAC addresses, at the cost of multi-homed source diversity.

**Behavior on uplink state change:** When the primary uplink fails and a secondary becomes the new primary, the new primary's prefix transitions from fully withdrawn (`AdvPreferredLifetime 0`, `AdvDefaultLifetime 0`) to advertised (`AdvPreferredLifetime`/`AdvDefaultLifetime` reflecting remaining state). A SIGHUP is sufficient — see §6.4.

**Trade-off:** This option sacrifices source-address diversity — all clients use addresses from, and route through, the same uplink simultaneously. A new connection established during a brief window where the old primary's RAs have expired but the new primary's RAs have not yet arrived may fail. This is a site-wide policy knob, not a per-client fix; use it only when RFC 6724 compliance cannot be relied on.

This is **disabled by default**.

---

## 10. Monitoring and State Machine

### 10.1 State Machine

Each uplink has **two independent state machines**: one for IPv4, one for IPv6. IPv6 state is only tracked if `ipv6_pd: true`.

States: `UP`, `DOWN`

Counters: `consecutive_failures` (used in UP state), `consecutive_successes` (used in DOWN state)

```
         ┌────────────────────────────────────────────────────────┐
         │                                                        │
         ▼     probe success                                      │
       ┌────┐  (reset consecutive_failures)                       │
  ───▶ │ UP │─────────────────────────────────────────────────────┤
       └────┘                                                      │
         │     probe failure                                       │
         │     (increment consecutive_failures)                    │
         │     consecutive_failures >= failure_threshold           │
         │     → run deprovisioning                                │
         ▼                                                        │
       ┌──────┐  probe failure                                     │
       │ DOWN │  (reset consecutive_successes)                     │
       └──────┘                                                     │
         │     probe success                                        │
         │     (increment consecutive_successes)                   │
         │     consecutive_successes >= recovery_threshold         │
         │     → run reprovisioning                                │
         └────────────────────────────────────────────────────────┘
```

Transition to DOWN: when `consecutive_failures` reaches `failure_threshold` (default: 3).
Transition to UP: when `consecutive_successes` reaches `recovery_threshold` (default: 3).

Counter semantics:
- In `UP` state: `consecutive_failures` increments on each failed probe, resets to 0 on any successful probe.
- In `DOWN` state: `consecutive_successes` increments on each successful probe, resets to 0 on any failed probe.
- On state transition, both counters reset to 0.

### 10.2 IPv4 Probe Detail

```sh
ping -c 1 -W 2 -I <wan-iface> <host>
```

- `-c 1`: Send one packet.
- `-W 2`: Wait 2 seconds for a reply.
- `-I <wan-iface>`: Bind to the WAN interface (forces use of the WAN default route for this uplink).

All hosts in `monitor.v4_hosts` are probed. The probe **passes** if any host responds (`exit 0`). The probe **fails** if all hosts fail.

The daemon runs probes sequentially within a probe cycle. All probes for all uplinks complete within a single `interval` period.

**Precondition for IPv4 probing:** The uplink's IPv4 state file (`/run/uplinkmgr/<name>.ipv4.state`) must exist (i.e., dhcpcd has successfully obtained an IPv4 lease). If the state file does not exist, the probe is skipped and the current state is maintained (not transitioned).

### 10.3 IPv6 Probe Detail

```sh
ping6 -c 1 -W 2 -I <wan-iface> <host>
```

- `-I <wan-iface>`: Binds the socket to the WAN interface. The kernel selects the route via the `ip -6 rule` chain installed by the daemon — normally the per-uplink routing table, falling through to the main table via the global `lookup main suppress_prefixlength 0` rule if the per-uplink table has no matching route. This is the correct behavior — it probes whatever path clients would actually use for this uplink, and degrades gracefully (rather than going blind) if the per-uplink table's route is temporarily missing.

All hosts in `monitor.v6_hosts` are probed. Pass/fail logic is the same as IPv4.

**Precondition for IPv6 probing:** `ipv6_pd: true` or `ia_na: true`. That's it — probing does **not** check whether the per-uplink routing table actually contains a route before attempting it; a probe failure (for any reason: no route in the per-uplink table, no route in the main table either, or a genuinely down interface) is itself the DOWN signal. Gating on route presence would mean the daemon can't observe an uplink failing in exactly the case it most needs to — see §17.

### 10.4 Probe Execution

The daemon uses Python's `subprocess` module to run ping commands directly; the `-W 2` timeout is enforced by ping itself.

Probes for different uplinks run **in parallel** using a `ThreadPoolExecutor` (one thread per uplink). Within each uplink's thread, IPv4 and IPv6 probes and their `ping_count` retry loops are sequential. All uplink threads are submitted at once; the daemon waits for all to complete before processing results and updating state machines. This bounds the cycle time to the slowest single uplink's probe sequence rather than the sum of all uplinks'.

---

## 11. Provisioning and Deprovisioning Sequences

### 11.1 IPv4 Deprovisioning (UP → DOWN)

1. Read the gateway from `/run/uplinkmgr/<name>.ipv4.state`.
2. Remove the IPv4 default route from the uplinkmgr table:
   ```sh
   ip route del default dev <wan-iface> table uplinkmgr
   ```
3. Log the event: `uplink <name> IPv4 DOWN after <N> consecutive failures`.

### 11.2 IPv4 Reprovisioning (DOWN → UP)

1. Read the gateway from `/run/uplinkmgr/<name>.ipv4.state`.
   - If the state file does not exist (dhcpcd has not yet obtained a lease), log a warning and skip. The daemon will install the route on the next SIGUSR1 from the hook when the lease arrives.
2. Install the IPv4 default route in the uplinkmgr table:
   ```sh
   ip route replace default via <GW4> dev <wan-iface> metric <metric> table uplinkmgr
   ```
3. Log the event: `uplink <name> IPv4 UP after <N> consecutive successes`.

### 11.3 IPv6 Deprovisioning (UP → DOWN)

1. Regenerate the radvd config for this uplink with DOWN-state parameters:
   - `AdvDefaultPreference low`
   - `AdvDefaultLifetime 0`
   - `AdvPreferredLifetime 0`
   - `AdvValidLifetime 0`
   - `DecrementLifetimes off`
   
   The regeneration algorithm for any given radvd config file:
   - Determine this uplink's state: DOWN.
   - Determine the "priority rank" among UP uplinks: not applicable (this uplink is DOWN).
   - Write the config with DOWN-state values.

2. Write the new config atomically:
   ```python
   tmp = f"/etc/radvd/radvd-uplinkmgr-{name}.conf.tmp"
   with open(tmp, 'w') as f:
       f.write(generated_config)
   os.rename(tmp, f"/etc/radvd/radvd-uplinkmgr-{name}.conf")
   ```

3. Send SIGHUP to the radvd instance:
   ```sh
   systemctl kill --signal=SIGHUP radvd-uplinkmgr-<name>.service
   ```

4. Optionally (if `reject_wrong_pd_src`): The `ipv6_reject_wrong_pd_src` rules are managed by the daemon based on state file presence; no additional action needed here.

5. Log the event: `uplink <name> IPv6 DOWN`.

**Note:** the `ipv6_fwd_to_uplink` rule is untouched by this transition — it's static for the daemon's lifetime (§7.5) and reacts to neither health-DOWN nor PD-state loss. Health-DOWN alone doesn't remove the per-uplink table's IPv6 default route either — that route is driven purely by `ipv6ra.state` presence (`_reconcile_uplink_ipv6`), independent of probe-based health. What actually stops clients from generating traffic toward a DOWN uplink is step 1 above: radvd's `AdvDefaultLifetime 0`/`AdvPreferredLifetime 0`/`AdvValidLifetime 0` tell clients to stop using this router and abandon its addresses. `ipv6_fwd_to_uplink` merely says "packets arriving on this macvlan consult this table" — harmless to leave in place, since it depends on the table actually containing a route (itself gated on live upstream RA/PD, not on daemon-observed health) to have any effect.

### 11.4 IPv6 Reprovisioning (DOWN → UP)

1. Regenerate **all** IPv6 uplink radvd configs (this uplink's recovery may change other uplinks' tiers), writing accurate remaining lifetimes from state files.

2. Write configs atomically and SIGHUP all IPv6 radvd instances (preference change only; radvd's live counters are unaffected).

3. Log the event: `uplink <name> IPv6 UP`.

### 11.5 radvd Config Regeneration Algorithm

The daemon uses this algorithm on both triggers (state change → SIGHUP; SIGUSR1 → restart):

```python
import ipaddress, time

now = time.time()
ipv6_uplinks = [u for u in uplinks if u.ipv6_pd]
up_ipv6_uplinks = sorted(
    [u for u in ipv6_uplinks if u.ipv6_state == 'UP'],
    key=lambda u: u.index
)
highest_priority_v6 = up_ipv6_uplinks[0] if up_ipv6_uplinks else None

for uplink in ipv6_uplinks:
    # Router lifetime (from upstream RA)
    gw_state = read_state(f"{uplink.name}.ipv6ra.state")
    nd1_remaining = (
        max(0, gw_state.lifetime - (now - gw_state.timestamp))
        if gw_state else 1800  # conservative fallback
    )

    # Delegated prefix and lifetime (from WAN BOUND6/RENEW6)
    pd_state = read_state(f"{uplink.name}.ipv6pd.state")

    if uplink.ipv6_state == 'DOWN':
        preference = 'low'
        nd1_remaining = 0  # withdraw as default router entirely
        preferred_lifetime = 0
        valid_lifetime = 1800
        prefix_info = _derive_prefix_info(pd_state, uplink, now)
    else:
        preference = 'high' if uplink == highest_priority_v6 else 'medium'
        prefix_info = _derive_prefix_info(pd_state, uplink, now)
        # exclusive_preferred_pd: only the primary UP uplink is visible to
        # clients as a default router at all -- secondaries are fully
        # withdrawn, not just dispreferred, since RFC 6724 rule 5.5 isn't
        # implemented in most stacks yet.
        if cfg.exclusive_preferred_pd and uplink != highest_priority_v6:
            preferred_lifetime = 0
            nd1_remaining = 0

    generate_radvd_config(uplink, preference, nd1_remaining,
                          preferred_lifetime, valid_lifetime, prefix_info)

def _derive_prefix_info(pd_state, uplink, now):
    """Return list of {iface, prefix, vltime, pltime} for each macvlan, or None entries."""
    if pd_state is None:
        return [None] * len(uplink.macvlan_ifaces)

    delegated = ipaddress.ip_network(
        f"{pd_state.delegated_prefix}/{pd_state.delegated_length}", strict=False
    )
    sla_bits = 64 - pd_state.delegated_length  # number of bits available for SLA IDs
    if len(uplink.macvlan_ifaces) > 2**sla_bits:
        log.warning(f"prefix delegation (/{pd_state.delegated_length}) too small "
                    f"for network count {len(uplink.macvlan_ifaces)}")
        return [None] * len(uplink.macvlan_ifaces)
    vltime = max(0, pd_state.vltime - (now - pd_state.timestamp))
    pltime = max(0, pd_state.pltime - (now - pd_state.timestamp))

    result = []
    for sla_id, iface in enumerate(uplink.macvlan_ifaces):
        # The SLA ID field always starts at bit 64 (immediately above the /64's
        # interface-identifier bits), regardless of the delegated prefix length --
        # its *width* (sla_bits) only bounds how many distinct sla_id values fit.
        subnet_addr = delegated.network_address + (sla_id << 64)
        prefix = ipaddress.ip_network(f"{subnet_addr}/64")
        result.append({'iface': iface, 'prefix': prefix, 'vltime': vltime, 'pltime': pltime})
    return result
```

### 11.6 AdvDefaultLifetime

`AdvDefaultLifetime` (the Router Lifetime field in the RA header) is set to the **remaining `lifetime`** from `<uplink-name>.ipv6ra.state` while the uplink is UP. This propagates the upstream router's validity window directly to clients. There is no separate `route ::/0` stanza — a Route Information Option (RFC 4191) for `::/0` would be redundant with the Router Lifetime/Preference already carried in the RA header itself, since `::/0` is definitionally the least-specific route possible (see §6.4).

`AdvDefaultLifetime` **is** zeroed on downstate, fully withdrawing the router from clients' default router list rather than leaving it advertised as an unreachable last resort. `AdvPreferredLifetime 0` and `AdvValidLifetime 0` together signal clients to immediately abandon addresses from the failed uplink's prefix. `DecrementLifetimes off` is always used (see §6.4); for the DOWN case it additionally ensures radvd keeps sending the invalidating RA rather than suppressing the prefix block. The DOWN-state `0` reaches clients via the same SIGHUP-only mechanism already relied on for the preferred/valid-lifetime zeroing (see §6.4).

It is also zeroed for any secondary UP uplink when `exclusive_preferred_pd: true` (see §9.6) — the same mechanism as DOWN, applied for a different reason: ensuring only one uplink is ever visible to clients as a default router, since RFC 6724 rule 5.5 isn't implemented in most stacks yet.

**Clamping:** because the remaining lifetime decays continuously between RA refreshes, the raw UP-state value routinely falls below what `radvd.conf(5)` allows for `AdvDefaultLifetime` (either exactly `0`, or between `MaxRtrAdvInterval` and 9000 seconds). `MaxRtrAdvInterval` is set explicitly to `300` on every generated interface block, and `AdvDefaultLifetime` is clamped to `0` or `[300, 9000]` (`generator._clamp_default_lifetime()`) before being written — an already-zero value (including the `lifetime == 0` "infinite" sentinel described in §5.3.3) passes through unclamped as `0`; distinguishing "genuinely expired" from "infinite" for that sentinel is deferred (see §17 item 20).

---

## 12. Boot-Time Behavior

### 12.1 Design Constraint

Debian 13's `network-online.target` (and systemd's `wait-online` logic) will cause a 5-minute boot timeout if the network appears to not be configured. To prevent this, IPv4 connectivity must be available **before** `uplinkmgr.service` starts and without depending on the daemon.

### 12.2 Boot Sequence

1. **ifupdown runs** (`/etc/init.d/networking start` or `networking.service`): Brings up all `auto` interfaces, including macvlan interfaces defined in `/etc/network/interfaces.d/uplinkmgr.conf`.

2. **`dhcpcd.service` starts** (the single system dhcpcd instance; config managed by `uplinkmgr-setup`). dhcpcd manages all uplink WAN interfaces and macvlan interfaces simultaneously:
   - Obtains IPv4 leases on each WAN interface.
   - Adds default routes to the main table with the configured metrics (dhcpcd's own behavior; serves as boot-time fallback).
   - Runs the dhcpcd hook for each event, which writes state files and signals the daemon.
   - (For `ipv6_pd: true` uplinks) Requests prefix delegation and sub-delegates to macvlan interfaces.

3. **`radvd-uplinkmgr-<name>.service` units start** (each depends on `dhcpcd.service`). radvd begins advertising prefixes on macvlan interfaces.

4. **`uplinkmgr.service` starts** (depends on `dhcpcd.service`). The daemon begins monitoring. Since step 2's hook events may have fired (and written state files) before this step — while nothing was running to receive their `SIGUSR1` signals — the daemon first runs `dhcpcd -g` (see §5.3.8 step 4) to force dhcpcd to replay hooks for its current state now that the daemon is ready to receive them, then performs its startup reconcile pass. At this point, routes are already configured; the daemon's initial state is `UP` for all uplinks.

**Result:** IPv4 connectivity is available as soon as dhcpcd obtains a lease on any uplink interface (step 2), long before the daemon starts. Debian's boot does not time out waiting for the network. The `dhcpcd -g` replay in step 4 closes the race where step 2's hook events fired before the daemon existed to act on them, so routing state settles on the daemon's very first reconcile pass rather than waiting for a later, unrelated dhcpcd event.

### 12.3 Route Redundancy at Boot

If both uplinks are functional at boot, both default routes are installed in the uplinkmgr table by the daemon's startup reconcile pass (once state files are present). The highest-priority uplink's route is selected by metric. The daemon, when it starts, confirms health and takes no deprovisioning action.

If an uplink fails before the daemon starts, dhcpcd either:
- Never obtains a lease (route never added), or
- Obtains a lease and adds the route, then the daemon removes it after 3 failures.

In the failure-before-daemon-start case, the bad route may briefly be active until the daemon deprovisions it. This is acceptable — boot-time recovery is secondary to availability.

---

## 13. Daemon Lifecycle and Cleanup

### 13.1 Startup

See §5.3.8.

### 13.2 Normal Operation

The daemon runs its monitoring loop continuously. All mutations to the system state are logged (to stderr/journald via the systemd unit).

### 13.3 Stop / SIGTERM Handling

When the daemon receives SIGTERM (or is stopped by `systemctl stop uplinkmgr`):

1. **Remove all installed routes and rules:**
   - Remove all IPv4 routes from the `uplinkmgr` table (one per uplink where a route was installed).
   - Remove all IPv4 default routes from per-uplink tables (used by `lo_to_uplink` rules).
   - Remove all IPv4 `lo_to_uplink` rules.
   - Remove the two global IPv4 policy rules (`suppress_prefixlength 0` and `lookup uplinkmgr`).
   - Remove all IPv6 default routes from per-uplink tables.
   - Remove all ip -6 rules (`fwd_to_uplink`, `lo_to_uplink`, optionally `ipv6_reject_wrong_pd_src`).
   - Remove the global IPv6 policy rule (`lookup main suppress_prefixlength 0`).

2. **Regenerate all radvd configs to "everything up" state:** Regenerate all radvd config files as if all IPv6 uplinks are UP. Assign preference tiers by uplink priority (index 0 = high, others = medium). Write all configs atomically.

3. **Send SIGHUP to all radvd instances:**
   ```sh
   systemctl kill --signal=SIGHUP radvd-uplinkmgr-<name>.service
   ```
   for each IPv6 uplink.

4. **Remove the PID file:**
   ```sh
   rm -f /run/uplinkmgr/uplinkmgr.pid
   ```

5. Exit 0.

**Rationale:** On daemon stop, all uplinkmgr-specific policy routing rules are removed. Traffic then falls through to the kernel's default `lookup main` rule and uses dhcpcd's main-table routes (with the configured metrics), providing full connectivity without the daemon. radvd configs are left in the optimistic state so advertisements continue correctly. This allows the administrator to stop the daemon for maintenance without disrupting connectivity.

### 13.4 What the Daemon Does NOT Clean Up

- State files in `/run/uplinkmgr/` (written by the hook; removed by the hook on EXPIRE/STOP events; also cleaned up by systemd's `RuntimeDirectory=` on service stop)
- macvlan interfaces (managed by ifupdown)
- dhcpcd leases or processes (managed by systemd)
- radvd processes (managed by systemd)
- nftables rules (administrator's responsibility)
- dhcpcd's main-table default routes (managed by dhcpcd; these serve as the fallback when the daemon is not running)

### 13.5 SIGHUP Handling (Daemon)

If the daemon itself receives SIGHUP, it reloads the config file and resets all state to UP. This is useful for applying config changes without a full restart.

---

## 14. Debian Package Structure

### 14.1 Package Name

`uplinkmgr`

### 14.2 Dependencies

```
Depends: python3 (>= 3.9), dhcpcd, radvd, iproute2, iputils-ping, ifupdown
```

**Notes:**
- `iputils-ping` provides `ping` and `ping6` (or `ping` with IPv6 support — confirm on Debian 13 (Trixie)).
- `ifupdown` is needed for the interfaces.d mechanism.
- Python 3.9+ is required for type hints and `importlib.resources` usage.

### 14.3 Installed File Paths

| File | Path |
|------|------|
| Daemon binary | `/usr/sbin/uplinkmgr` |
| Setup binary | `/usr/sbin/uplinkmgr-setup` |
| dhcpcd hook | `/usr/libexec/dhcpcd-hooks/50-uplinkmgr` |
| systemd service | `/lib/systemd/system/uplinkmgr.service` |
| Default config | `/etc/uplinkmgr/uplinkmgr.yaml` (not overwritten on upgrade) |
| Debconf templates | `/usr/share/uplinkmgr/templates` |

Generated files (written by `uplinkmgr-setup`, not by the package directly) are listed in §5.1.3.

### 14.4 `uplinkmgr.service`

```ini
[Unit]
Description=uplinkmgr multi-WAN uplink monitor daemon
After=network.target dhcpcd.service

[Service]
Type=simple
ExecStart=/usr/sbin/uplinkmgr --log-clean
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure
RestartSec=10s
RuntimeDirectory=uplinkmgr
RuntimeDirectoryMode=0755

[Install]
WantedBy=multi-user.target
```

`RuntimeDirectory=uplinkmgr` causes systemd to create `/run/uplinkmgr/` with the correct permissions before starting the daemon, and clean it up on stop.

`--log-clean` (§5.3.2) is passed because this unit's output is always captured by journald, which already timestamps and identifies each line.

### 14.5 `postinst` Script

The `postinst` script (run after package installation) performs:

1. If `/etc/uplinkmgr/uplinkmgr.yaml` does not exist, install the default example config.
2. Run `uplinkmgr-setup` to generate all config files, including writing `/etc/dhcpcd.conf` (with backup).
3. Reload systemd daemon: `systemctl daemon-reload`.
4. Restart `dhcpcd.service` to pick up the new `/etc/dhcpcd.conf`.
5. Enable and start each generated `radvd-uplinkmgr-*.service` unit.
6. Enable and start `uplinkmgr.service`.

On upgrade (`$1 = configure` with a previous version), `postinst`:
1. Runs `uplinkmgr-setup` to regenerate files (picks up any format changes).
2. Reloads systemd daemon.
3. Restarts `dhcpcd.service` and any already-running `radvd-uplinkmgr-*.service` units.

### 14.6 `prerm` and `postrm` Scripts

`prerm` (run before files are removed):
- Stops and disables all `radvd-uplinkmgr-*.service` units.
- Stops `dhcpcd.service` so it is not running against a stale config while postrm restores the backup.

`postrm` on remove or purge:
- Restores `/etc/dhcpcd.conf` from `/etc/dhcpcd.conf.pre-uplinkmgr` if the backup exists; otherwise removes the generated `/etc/dhcpcd.conf`.
- Removes all other generated files (`/etc/network/interfaces.d/uplinkmgr.conf`, `/etc/systemd/system/radvd-uplinkmgr-*.service`, `/etc/radvd/radvd-uplinkmgr-*.conf`, `/etc/iproute2/rt_tables.d/uplinkmgr.conf`).
- On purge: also removes `/etc/uplinkmgr/` (including the user's `uplinkmgr.yaml`).
- Reloads systemd.

### 14.7 `dpkg-reconfigure` Support

Debconf is used to display a confirmation prompt when `dpkg-reconfigure uplinkmgr` is run. The `config` script prompts: "Run uplinkmgr-setup to regenerate config files from /etc/uplinkmgr/uplinkmgr.yaml? [yes/no]". If yes, `postinst` calls `uplinkmgr-setup`.

### 14.8 Python Packaging

The Python components (`uplinkmgr` daemon and `uplinkmgr-setup`) are installed as executable scripts pointing to Python modules. Using `dh_python3` for build-time dependency handling. The package does not use a virtual environment — it relies on system Python.

Module structure:
```
/usr/lib/python3/dist-packages/uplinkmgr/
    __init__.py
    config.py       # YAML config parsing and validation
    naming.py       # Naming convention utilities (macvlan names, table numbers, paths)
    priority.py     # ip rule priority allocation for all rule types
    generator.py    # Config file generation (used by uplinkmgr-setup and daemon)
    daemon.py       # Main daemon loop
    monitor.py      # Probe logic
    statemachine.py # Uplink state machine
    routing.py      # ip route/rule manipulation
    radvd.py        # radvd config generation and SIGHUP
    state.py        # State file reading (IPv4State, IPv6RaState, etc.)
```

`/usr/sbin/uplinkmgr` and `/usr/sbin/uplinkmgr-setup` are thin entry-point scripts.

---

## 15. Comparison with systemd-networkd

### 15.1 Overview

systemd-networkd is the networking daemon included with systemd, which provides interface management, DHCP client/server, IPv6 RA handling, and basic policy routing. This section evaluates whether it could replace the ifupdown + dhcpcd approach used by uplinkmgr.

### 15.2 What systemd-networkd Can Do Natively (Debian 13 (Trixie))

**Interface management via .netdev and .network files:**
- Macvlan interfaces can be created declaratively using `.netdev` files with `Kind=macvlan`.
- IPv4 and IPv6 DHCP are supported natively in `.network` files (`DHCP=yes`, `DHCP=ipv4`, `DHCP=ipv6`).
- No need for ifupdown `pre-up`/`up`/`down` stanzas.

**IPv6 Prefix Delegation:**
- Since systemd 246 (available in Debian 11+), `.network` files support `DHCPPrefixDelegation=yes` on the WAN interface and `IPv6PrefixDelegationConfig=DHCPv6` on downstream interfaces.
- This is functional on Debian 13 (Trixie), but has had bugs and behavioral changes across systemd versions.
- SLA IDs can be specified via `SubnetId=` in the downstream interface's `.network` file.

**Policy routing:**
- `.network` files support `RoutingPolicyRule` sections for both IPv4 and IPv6.
- This can express `ip rule` equivalents, including `IncomingInterface=` (iif).
- Rules are automatically added and removed when the interface comes up/down.

**Route metrics:**
- `RouteMetric=` can be set per interface in `.network` files, controlling the metric of DHCP-assigned default routes.

**Per-uplink routing tables:**
- Can be specified in `.network` files using `Table=` in `[Route]` sections.

**Summary:** systemd-networkd can handle macvlan creation, DHCP, IPv6 PD sub-delegation, metric-ordered default routes, and policy routing rules — all without external tools.

### 15.3 What systemd-networkd Cannot Do or Does Poorly

**radvd is still external:**
- systemd-networkd does not include a router advertisement daemon for downstream interfaces. A separate radvd (or `ndisc6`/`ndppd`) instance is still required for advertising prefixes to clients via SLAAC.
- `AdvDefaultPreference` management based on uplink health still requires custom code regardless of which DHCP client is used.
- The fine-grained radvd lifecycle management (SIGHUP on config change, per-uplink config regeneration) is the same complexity whether using systemd-networkd or dhcpcd.

**networkd-dispatcher is less mature than dhcpcd hooks:**
- `networkd-dispatcher` provides event hooks analogous to dhcpcd's exit hooks, but it is a separate package (`networkd-dispatcher`), must be installed separately, and is less widely tested for complex multi-interface scenarios.
- The dhcpcd hook model is more mature, better documented for complex PD scenarios, and has a richer event taxonomy (BOUND, RENEW, REBIND, ROUTERADVERT, BOUND6, etc.).
- networkd-dispatcher fires on interface state changes (routable, degraded, etc.), not on DHCP lease events directly, which is a coarser granularity.

**systemd-resolved conflicts:**
- systemd-networkd is tightly integrated with systemd-resolved, which replaces `/etc/resolv.conf` with a stub resolver (`127.0.0.53`). This conflicts with the conventional DHCP-managed DNS in `/etc/resolv.conf`.
- On a router serving DNS to clients (e.g., via dnsmasq or a forwarding resolver), this creates a complex layering issue that requires explicit configuration to bypass.
- dhcpcd writes directly to `/etc/resolv.conf` or a managed file, which is simpler for router use cases.

**Debug visibility is lower:**
- With ifupdown + dhcpcd, the entire configuration is explicit in text files under `/etc/network/interfaces.d/` and `/etc/dhcpcd-uplinkmgr-*.conf`. Each step can be inspected, replayed, and debugged independently.
- With systemd-networkd, the effective configuration is a composite of `.network` and `.netdev` files processed by a daemon. `networkctl status`, `journalctl -u systemd-networkd`, and `networkctl lldp` provide inspection, but there is no equivalent of "run this one command and see what happens."
- For a multi-uplink router with complex policy routing, this opacity increases the difficulty of diagnosing subtle routing bugs.

**IPv6 PD experimental status:**
- The `DHCPPrefixDelegation=` feature has had bugs and behavioral changes across systemd versions (241 through 255+). On a stable Debian 13 (Trixie) system that will not receive updated systemd until the next major release, bugs in PD handling may not be patched.
- dhcpcd has a long track record with IPv6 PD on embedded and router use cases.

**Migration cost:**
- The ifupdown + dhcpcd approach is already familiar to Debian administrators. Switching to systemd-networkd requires learning a new configuration language and debugging toolchain.
- On a running production router, migrating from ifupdown to systemd-networkd carries real risk of network outage.

### 15.4 Version-Specific Notes (Debian 13 (Trixie))

| Feature | Status |
|---------|----------------------|
| Macvlan via .netdev | Stable |
| DHCP (IPv4 and IPv6) | Stable |
| DHCPv6 PD (outbound request) | Functional, previously experimental |
| DHCPv6 PD downstream sub-delegation | Functional (SubnetId=), some edge case bugs |
| RoutingPolicyRule (IPv4 and IPv6) | Functional |
| Per-table routes via Table= | Functional |
| RouteMetric= | Stable |
| networkd-dispatcher | Separate package, functional |
| Integration with systemd-resolved | Enabled by default (may conflict) |

### 15.5 Conclusion

systemd-networkd is now viable for the static/semi-permanent configuration aspects of uplinkmgr (interface creation, DHCP, PD, policy routing rules). It is no longer as immature as it was in Debian 9/10.

However:
1. **It does not eliminate the need for the uplinkmgr daemon.** The daemon's monitoring, radvd lifecycle management, and AdvDefaultPreference logic are required regardless of which DHCP client is used. The daemon complexity is the same either way.
2. **The migration cost is non-trivial.** Switching from ifupdown to systemd-networkd on a production router requires testing and carries outage risk.
3. **Debug visibility is higher with ifupdown + dhcpcd.** For a complex multi-uplink router, this is a meaningful operational advantage.
4. **systemd-resolved integration is a net negative** for a router use case, and requires careful configuration to avoid conflicts.

**Decision:** ifupdown + dhcpcd is the right choice for this project. The decision is not based on systemd-networkd being immature — it is based on a better fit for the operational characteristics of a router, better debuggability, and a simpler migration path from standard Debian networking.

---

## 16. Constraints, Invariants, and Known Limitations

### 16.1 Hard Constraints

1. **Interface name length:** All derived macvlan names must be ≤ 15 characters. `uplinkmgr-setup` enforces this.
2. **Routing table number uniqueness:** Table numbers `[routing_table_start, routing_table_start + len(uplinks)]` must not conflict with any existing table definitions.
3. **Uplink name uniqueness:** Uplink names must be unique. This is enforced at config parse time.
4. **dhcpcd interface restriction:** The single dhcpcd instance uses `allowinterfaces` in the generated `/etc/dhcpcd.conf` to restrict management to exactly the WAN and macvlan interfaces listed by uplinkmgr-setup. This prevents dhcpcd from autonomously configuring any other interface on the system.
5. **Hook idempotency:** The dhcpcd hook must be safe to run multiple times for the same event (e.g., RENEW after BOUND). It writes state files atomically (write to `<file>.tmp`, then `mv` to `<file>`) and signals the daemon; the daemon's reconcile logic is inherently idempotent (`ip route replace` is atomic; rules are only installed if not already present with the same parameters).
6. **Daemon optimistic start:** The daemon must not deprovision uplinks at startup. Routes are assumed to be correctly configured by dhcpcd before the daemon starts.

### 16.2 Ordering Invariants

- `uplinkmgr-setup` must be run before dhcpcd or any radvd instance is started (it generates their configs).
- dhcpcd must be running before the uplinkmgr daemon starts (daemon reads state files written by the hook).
- radvd instances must be running before clients attempt SLAAC (they need to receive RAs immediately on link-up).

### 16.3 Known Limitations

1. **DHCP-only WAN:** Only DHCP WAN uplinks are supported. PPPoE and static WAN configurations are out of scope.
2. **IPv6 PD only:** IPv6 is only provisioned if the WAN provides prefix delegation. Uplinks with `ipv6_pd: false` are IPv4-only; there is no fallback to SLAAC-from-WAN or static IPv6.
3. **Single delegated prefix per uplink:** The design assumes one prefix delegation per uplink. If an ISP provides multiple prefixes, only the first is used.
4. **Within-uplink probe sequencing:** Probes for different uplinks run in parallel (one thread per uplink). Within a single uplink's thread, IPv4 and IPv6 probes are sequential. With many probe hosts or a high `ping_count`, a cycle for one uplink could take longer than `monitor.interval`. The daemon logs a warning if any cycle exceeds the interval.
5. **radvd `prefix ::/64` fallback:** The initial radvd config (generated by `uplinkmgr-setup`) uses `prefix ::/64` as a placeholder until the first PD is received. If radvd cannot derive the delegated prefix automatically from the macvlan interface's assigned address, clients will not receive a useful prefix until the daemon regenerates the config after the first SIGUSR1 from the hook. This is a boot-time-only window.
6. **Restart gap on lifetime refresh:** When the daemon restarts a radvd instance to apply fresh lifetimes (on SIGUSR1), there is a brief window (typically < 1 second) during which radvd is not sending RAs. Clients will not notice a gap this short. There is also a sub-second race between the daemon computing remaining lifetimes and radvd starting its countdown from those values, meaning advertised lifetimes may be very slightly longer than the upstream values.
7. **Asymmetric uplink indices after config change:** If an uplink is removed from the middle of the `uplinks:` list, all subsequent uplinks' indices, MACs, link-locals, and routing table numbers change. This requires a full re-run of `uplinkmgr-setup`, restart of all affected services, and the administrator should be warned that existing client addresses become stale.
8. **NAT not automatic:** `uplinkmgr-setup` generates a reference nftables fragment at `/etc/uplinkmgr/uplinkmgr-nat.nft.example` but does not apply it. The administrator is responsible for incorporating NAT rules into their firewall configuration.

### 16.4 Security Considerations

- The dhcpcd hook script runs as root (dhcpcd runs as root). The env files in `/etc/uplinkmgr/uplinks/` must be readable only by root (`chmod 600`) since they could be used to influence hook behavior.
- `/run/uplinkmgr/` contains gateway IP addresses (state files). These are not sensitive but should be owned by root.
- uplinkmgr does not configure any firewall rules. The administrator is responsible for NAT and inbound filtering. The reference fragment at `/etc/uplinkmgr/uplinkmgr-nat.nft.example` provides a starting point for masquerade rules.

---

## 17. Open Verification Items

The following items require verification against upstream documentation or testing on the target platform (Debian 13 (Trixie), dhcpcd 10.1, radvd 2.19) before implementation.

| # | Component | Item | Where to Verify |
|---|-----------|------|----------------|
| 1 | dhcpcd hook | ~~Exact variable names for IPv6 PD prefix, vltime, and pltime~~ **Confirmed:** PD variables are on the **WAN interface** BOUND6/RENEW6 event (not macvlan events): `$dhcp6_ia_pd1_prefix1`, `$dhcp6_ia_pd1_prefix1_length`, `$dhcp6_ia_pd1_prefix1_vltime`, `$dhcp6_ia_pd1_prefix1_pltime`. Delegated prefix length may be less than 64 (e.g. /60). | — |
| 2 | dhcpcd hook | ~~Variable holding the RA source address and router lifetime for `ROUTERADVERT` events~~ **Confirmed:** `$nd1_from` (gateway / RA source address), `$nd1_lifetime` (router lifetime in seconds; note the state file key is `lifetime=`, not `nd1_lifetime=`). Also confirmed: `$nd1_flags` (flag characters including `M` for managed), `$nd1_addr1` (first SLAAC address), `$nd1_prefix_information1_prefix` (RA prefix address), `$nd1_prefix_information1_length` (RA prefix length). | — |
| 3 | dhcpcd config | ~~`ia_pd` directive syntax~~ **Confirmed:** `ia_pd <IAID>/<requested-prefix>/<hint-length> <iface>/<SLA-ID>/64 ...` — e.g., `ia_pd 2/::/56 vlan10-u0/0/64 vlan20-u0/1/64`. `ia_na <IAID>` is also required to obtain an IPv6 address on the WAN interface. `ipv6rs` is needed to trigger ROUTERADVERT events. `duid` ensures consistent lease assignment. | — |
| 4 | dhcpcd config | ~~`allowinterfaces` directive~~ **Confirmed:** `allowinterfaces` is correct in dhcpcd 10.x. | — |
| 5 | dhcpcd binary | ~~Correct path on Debian 13 (Trixie)~~ **Confirmed:** `/usr/sbin/dhcpcd`. | — |
| 6 | dhcpcd binary | ~~Flags `--config`, `--pidfile`, `--nobackground` in dhcpcd 10.x~~ **Confirmed:** `--config` and `--nobackground` are correct. `--pidfile` is not accepted — dhcpcd writes its pid to `/run/dhcpcd/<iface>.pid` when an interface is given as a positional argument. Systemd unit updated accordingly. | — |
| 7 | radvd | ~~Whether radvd re-reads config on SIGHUP~~ **Confirmed:** radvd re-reads config on SIGHUP (standard Unix daemon behavior; consistent with item 17 — counters continue from where they were). Note: dhcpcd does **not** support config reload via SIGHUP; `systemctl restart` is required when the dhcpcd config changes (only after re-running `uplinkmgr-setup`). | — |
| 8 | radvd | ~~Whether `prefix ::/64` with `AdvRouterAddr on` causes radvd to auto-use the interface's assigned /64 prefix~~ **Assumed correct:** each macvlan interface has exactly one global address assigned via PD, so radvd should pick it up unambiguously. To be verified by testing. | — |
| 9 | radvd binary | ~~Correct command-line flags: `--nodaemon`, `--config`, `--pidfile`, `--configtest` on Debian 13's radvd 2.19~~ **Confirmed:** all four flags exist and are valid, but `--nodaemon` must **not** be combined with `Type=forking` (it prevents radvd from forking, so systemd can't track it via `PIDFile`), and `--configtest` must run via `ExecStartPre`, not a second `ExecStart` (`Type=forking` permits only one `ExecStart` directive). See §6.5. | — |
| 10 | ifupdown | ~~Whether `inet manual` ifaces with only `pre-up`/`up`/`down` stanzas are processed correctly; whether `auto` is needed~~ **Confirmed:** `inet manual` ifaces accept `pre-up`/`up`/`down`/`post-down` stanzas; `auto` is required to activate them at boot. | — |
| 11 | nftables | ~~Whether `/etc/nftables.d/` exists and is included by default in Debian 13's `/etc/nftables.conf`~~ **Resolved:** `/etc/nftables.d/` is not created by the nftables package and is not loaded by default. nftables.service may also be disabled on install. NAT configuration is left entirely to the administrator; uplinkmgr-setup generates a reference fragment only. | — |
| 12 | iproute2 | ~~Behavior when `ip route add` is called for a route that already exists~~ **Confirmed:** `ip route add` errors on a duplicate route. All route installation uses `ip route replace` throughout. | — |
| 13 | kernel | ~~Behavior of `addr_gen_mode=1` set in `pre-up` — whether it persists after the interface is deleted and recreated~~ **Resolved:** set it unconditionally in every `pre-up` stanza regardless of prior state. | — |
| 14 | ping | ~~Whether `ping6` is available separately or merged into `ping` on Debian 13's `iputils-ping`~~ **Confirmed:** `ping6` is provided by `iputils-ping`. | — |
| 15 | dhcpcd | ~~Whether running uplinkmgr's dhcpcd alongside a system-default dhcpcd instance would cause conflicts~~ **Resolved:** conflicts are avoided by design. uplinkmgr uses a single dhcpcd instance (the system `dhcpcd.service`) with `allowinterfaces` restricting it to uplinkmgr's interfaces. The system-default dhcpcd config is replaced by `postinst`; no separate per-uplink dhcpcd process is involved. | — |
| 16 | iproute2 | ~~Whether `ip -6 route replace … expires <seconds>` is valid syntax for setting route expiry in iproute2 on Debian 13 (Trixie)~~ **Confirmed:** correct syntax. | — |
| 17 | radvd | ~~Whether radvd resets `DecrementLifetimes` counters to the config-file values on SIGHUP, or continues counting from where they were~~ **Confirmed:** SIGHUP does not reset counters; they continue decrementing from where they were. However, `DecrementLifetimes on` is not used: radvd advertises `min(configured_lifetime, address_lifetime_on_interface)`, and since dhcpcd keeps macvlan address lifetimes current, the interface address IS the countdown. `DecrementLifetimes off` is set universally; SIGHUP is sufficient for all radvd config updates including lifetime changes. | — |
| 18 | radvd | ~~Whether empty `RDNSS { };` / `DNSSL { };` stanzas are valid radvd config~~ **Confirmed: not valid.** radvd rejects empty `RDNSS`/`DNSSL` blocks. Since uplinkmgr has no config option that ever populates DNS server or search-domain content, these stanzas are omitted from generated radvd config entirely rather than emitted empty. | — |
| 19 | dhcpcd config | ~~Whether the default (unset) IAID is safe for macvlan interfaces~~ **Confirmed: not safe.** Per `dhcpcd.conf(5)`, the default IAID is derived from the interface's VLAN ID (or, failing that, the last 4 bytes of its MAC address); both are shared across every uplink's macvlan for a given network interface (all children of the same VLAN device), causing IAID collisions on the same L2 segment. Each macvlan now gets an explicit `interface <mv> { iaid ... }` stanza with a unique IAID (`naming.macvlan_iaid()`). See §6.2. | — |
| 20 | radvd | ~~Whether an unbounded, continuously-decaying `AdvDefaultLifetime` is valid~~ **Confirmed: not valid.** Per `radvd.conf(5)`, `AdvDefaultLifetime` must be either exactly `0` or between `MaxRtrAdvInterval` (radvd default: 600s, previously never overridden here) and 9000 seconds; the live remaining-lifetime value decays continuously and routinely lands in the forbidden 1-second-to-floor gap. Fixed by explicitly setting `MaxRtrAdvInterval 300;` and clamping `AdvDefaultLifetime` to `0` or `[300, 9000]` (`generator._clamp_default_lifetime()`). **Known follow-up, deliberately deferred:** the `lifetime == 0` "infinite" sentinel (§5.3.3) is indistinguishable from a genuinely-expired `0` in this clamp and passes through unclamped as `0`, which radvd reads as "not a default router" rather than "infinite" — not addressed by this fix. See §6.4, §11.6. | — |
| 21 | dhcpcd config | ~~Whether dhcpcd needs to be told macvlan interfaces are IPv6-only~~ **Confirmed: yes.** Per `dhcpcd.conf(5)`, `ipv6only` ("Only configure IPv6") is a valid bare per-interface directive. Without it, dhcpcd applies its default per-interface behavior to macvlans too, including IPv4 DHCP/ARP and falling back to an IPv4 link-local (APIPA) address on failure — unwanted for interfaces that only ever carry a delegated IPv6 `/64`. Added to every macvlan `interface` stanza (not the WAN uplink stanzas). See §6.2. | — |
| 22 | radvd | ~~Whether the explicit `route ::/0 { ... }` stanza (RFC 4191 Route Information Option) adds anything beyond `AdvDefaultPreference`/`AdvDefaultLifetime`~~ **Resolved: redundant.** Per RFC 4191 §2.1, a Route Information Option for `::/0` conveys the same "this router, for this long, at this preference" information already carried by the RA header's own Router Lifetime/Preference fields, since `::/0` is definitionally the least-specific route possible. Removed entirely from generated radvd config, along with the now-fully-unused `route_lifetime`/`AdvRoutePreference`/`AdvRouteLifetime`. See §6.4, §11.6. | — |
| 23 | radvd | ~~Whether zeroing `AdvDefaultLifetime` on DOWN takes effect via the existing SIGHUP-only transition path, or requires a full restart~~ **Resolved (per user direction):** SIGHUP is sufficient — the DOWN transition already relied on SIGHUP alone (`daemon.py` never calls `regenerate_all(..., action="restart")` for a state change) to apply `AdvPreferredLifetime 0`/`AdvValidLifetime 0` immediately, and `AdvDefaultLifetime` is rendered by the same `_radvd_interface_block()` call into the same config file, so it reloads via the identical mechanism. This corrects an earlier, narrower claim in §6.4 that a full restart was needed for lifetime changes — that claim did not hold given the DOWN transition's existing, already-shipped reliance on SIGHUP alone. See §6.4, §11.6. | — |
| 24 | radvd | ~~Whether `exclusive_preferred_pd` should zero `AdvDefaultLifetime` for secondary UP uplinks, not just `AdvPreferredLifetime`~~ **Resolved (per user direction):** yes — the purpose of `exclusive_preferred_pd` is that only one uplink should ever be visible to clients at all, because RFC 6724 rule 5.5 (source/router selection matching outgoing interface) isn't implemented in most stacks yet; a client that never implemented it could still pick a merely-dispreferred secondary uplink as a default router. Secondary UP uplinks now get `AdvDefaultLifetime 0` in addition to `AdvPreferredLifetime 0` (`radvd.py`'s `is_secondary` check, extending the same zeroing already applied for `AdvPreferredLifetime`). See §9.6, §11.6. | — |
| 25 | dhcpcd hook | ~~Whether a `dhcpcd -g`/`--reconfigure`-triggered `RECONFIGURE` hook invocation populates the same `new_*`/`nd1_*` variables as the corresponding real event (IPv4 routers/address, RA gateway/lifetime/prefix, DHCPv6 PD/IA_NA), for one variable group, several at once, or requires separate `-4`/`-6`-scoped invocations~~ **Unconfirmed, assumed defensive-safe:** `dhcpcd(8)`/`dhcpcd-run-hooks(8)` document the `-g` flag and `RECONFIGURE` reason but not its exact variable population. The hook's `RECONFIGURE` handler (§5.2.9) calls all three per-reason handlers unconditionally, and each independently no-ops when its own variables are absent, so it degrades gracefully regardless of the answer — but full replay coverage (the actual goal of §5.3.8 step 4) depends on it. To be verified by testing. | — |
| 26 | routing | ~~Whether the per-macvlan `ipv6_fwd_to_uplink` rule needs to be removed when PD state is lost or an uplink goes DOWN, or can stay installed for the daemon's lifetime~~ **Resolved (per user direction):** the rule can be static. It merely says "packets arriving on this macvlan consult this table" — the per-uplink table's actual route content (gated on live `ipv6ra.state`, independent of daemon-observed health) and radvd's RA updates (`AdvDefaultLifetime 0` etc. on DOWN) are what control whether traffic can or should flow, not the rule's mere presence. When PD is lost, a stale `from <old-delegated-prefix>` constraint (under `reject_wrong_pd_src`) is left in place rather than reverted — inert for the same reason, since radvd will already have told clients to abandon that prefix. Installed once, unconditionally, at startup (`Daemon._setup_ipv6_macvlan_rules()`); only ever updated in place (never deleted) until `_teardown_all()`. Scoped to `ipv6_fwd_to_uplink` only — the `ipv6_reject_wrong_pd_src` prohibit rule (a security control, not a routing-availability one) remains driven by PD-state presence as before. See §7.5, §11.3. | — |
| 27 | dhcpcd | ~~Whether `dhcpcd -g`/`--reconfigure` is safe to invoke when no dhcpcd master process is running yet~~ **Resolved: not safe, now gated.** Two e2e tests (`tests/e2e/test_ipv6.py::TestIPv6PD::test_ipv6_fwd_to_uplink_rule_installed`, `::test_ipv6_down_preference_low_in_radvd_conf`) start the uplinkmgr daemon before dhcpcd and failed with `isp1.ipv4.state` never being written. `dhcpcd(8)`'s other client-mode flags (`-n`/`--rebind`, `-N`/`--renew`) document "if dhcpcd is not running, then it starts up as normal" — `-g` isn't documented either way, but the failure pattern is consistent with the same fallback applying: a stray dhcpcd master started via the system default `/etc/dhcpcd.conf` (not the caller's intended config) blocking the real, correctly-configured dhcpcd from ever obtaining a lease. Not independently confirmed against a live dhcpcd (not testable from within this environment), so `Daemon._reconfigure_dhcpcd()` (§5.3.8 step 4) now checks `/run/dhcpcd/pid` for a live PID via `Daemon._dhcpcd_is_running()` before invoking `dhcpcd -g` at all, and skips (logging at debug level) if no live master is found — eliminating the risk category regardless of the exact mechanism. A 10s `subprocess` timeout was also added as defense-in-depth against any other stalling mode. | — |
| 28 | daemon | ~~Whether IPv6 probing should be gated on the per-uplink routing table already containing a default route~~ **Resolved: no, removed.** `tests/e2e/test_ipv6.py::TestIPv6PD::test_ipv6_down_preference_low_in_radvd_conf` failed: after `ip link set wan1 down`, the daemon never transitioned `isp1`'s IPv6 health to DOWN. Root cause: Linux purges IPv6 routes tied to a device from every routing table on `NETDEV_DOWN` (`rt6_ifdown()`), independent of uplinkmgr; the old precondition (§5.3.7/§10.3) then saw no route in the per-uplink table and silently skipped IPv6 probing every cycle thereafter, and nothing re-evaluated the precondition without a probe having already run — permanently freezing health at `UP`. The precondition was never actually necessary: `_reconcile_uplink_ipv6()` reinstalls the per-uplink route purely from `ipv6ra.state` (§11.3), independent of probe-observed health, and a probe simply fails on its own (via the kernel's `ip -6 rule` fallback to the main table, or outright) when nothing is reachable — that failure is itself the correct DOWN signal. `Daemon._probe_uplink()` now probes IPv6 unconditionally whenever `ipv6_pd`/`ia_na` is configured, with no route-existence check (`monitor.ipv6_default_route_exists()` removed entirely). Recovery is unaffected: once probes succeed again, the existing `ipv6_changed` → `_reconcile_uplink_ipv6()` path (unchanged) reinstalls the per-uplink route as a side effect. See §5.3.7, §10.3, §11.3. | — |
| 29 | daemon CLI | ~~Whether the journald-clean log format should be a config-file option or a CLI flag, and whether it should default on or off~~ **Resolved (per user direction):** CLI flag, consistent with the existing `--log-level`/`--config`/`--state-dir` flags (all CLI-only; the YAML config governs uplink/routing/monitoring semantics, not process-level logging). Defaults off, since a manual/terminal invocation still wants a self-contained line with its own timestamp; the packaged `uplinkmgr.service` unit (§14.4) passes `--log-clean` explicitly, since that unit's output is always captured by journald. See §5.3.2. | — |

---

*End of specification.*
