# uplinkmgr

A multi-WAN uplink management daemon for Linux routers running Debian 13
(Trixie). It handles automatic IPv4 failover and simultaneous multi-homed
IPv6 prefix delegation across multiple WAN uplinks.

## The problem

A home or small-office router with two or more WAN connections (cable,
fiber, Starlink, etc.) needs to do several things automatically:

- **IPv4 failover**: Route outbound traffic through a working uplink, detect
  failures quickly, and switch to a backup with minimal disruption.
- **Multi-homed IPv6**: Advertise delegated prefixes from each uplink
  simultaneously so that clients can reach the internet via any healthy
  uplink, using RFC 6724 source address selection to pick the right one.
- **Graceful degradation**: When an uplink fails, drain clients away from its
  IPv6 prefix without breaking existing connections, rather than suddenly
  withdrawing the prefix.

The Linux kernel's policy routing engine can do all of this, but wiring it
up correctly — routing tables, `ip rule` entries, per-uplink macvlan
interfaces, radvd config, dhcpcd prefix delegation, and failure detection —
requires substantial glue that does not exist in any stock package.
uplinkmgr provides that glue.

## Assumptions

uplinkmgr is designed around the standard Debian networking stack and
delegates to existing tools rather than replacing them:

- **ifupdown** manages all network interfaces — LAN bridges, VLANs, and the
  macvlan interfaces that uplinkmgr-setup generates. Interfaces are defined
  in `/etc/network/interfaces` and `/etc/network/interfaces.d/`. uplinkmgr
  does not bring interfaces up or down itself.

- **dhcpcd** (package `dhcpcd5`) handles DHCP and DHCPv6 on WAN interfaces,
  including requesting IPv6 prefix delegation and sub-delegating subnets to
  macvlan interfaces. uplinkmgr-setup generates `/etc/dhcpcd.conf`; the hook
  at `/lib/dhcpcd/dhcpcd-hooks/50-uplinkmgr` is the handoff point between
  dhcpcd events and the daemon.

- **radvd** sends Router Advertisements to internal networks. uplinkmgr-setup
  generates one radvd config file per IPv6-capable uplink and a parameterised
  systemd template unit. The daemon controls `AdvDefaultPreference` by
  rewriting those config files and sending SIGHUP to the radvd instances.

uplinkmgr does not support systemd-networkd, NetworkManager, or ISC
dhclient as replacements for any of the above.

## How it works

uplinkmgr has three components:

1. **`uplinkmgr-setup`** — a one-shot config generator. Run after editing
   `/etc/uplinkmgr/uplinkmgr.yaml` to regenerate dhcpcd config, macvlan
   interface stanzas, radvd config files, routing table names, and systemd
   units for radvd instances.

2. **dhcpcd hook** (`/lib/dhcpcd/dhcpcd-hooks/50-uplinkmgr`) — sourced by
   dhcpcd on every lease event. Writes compact state files to
   `/run/uplinkmgr/` (gateway IP, delegated prefix, RA lifetime) and sends
   `SIGUSR1` to the daemon to trigger reconciliation.

3. **`uplinkmgr` daemon** — runs continuously. On `SIGUSR1`, reads state
   files and installs or updates routes and `ip rule` entries. Probes each
   uplink by pinging known hosts; on repeated failure, removes the uplink's
   routes and adjusts radvd's `AdvDefaultPreference` to steer clients away.
   Restores the uplink on recovery.

## Quick-start configuration

### 1. Write `/etc/uplinkmgr/uplinkmgr.yaml`

```yaml
uplinkmgr:
  networks:
    - name: lan
      interface: vlan10      # your LAN-facing bridge or VLAN interface

  uplinks:
    - name: comcast
      interface: eth0        # WAN interface for this uplink
      metric: 100            # lower metric = higher IPv4 preference
      ipv6_pd: true          # request DHCPv6 prefix delegation

    - name: starlink
      interface: eth1
      metric: 200
      ipv6_pd: false         # IPv4 only; no IPv6 PD on this uplink
```

Multiple networks and uplinks are supported. The `ipv6_pd: true` uplinks
each get a macvlan interface per network, a per-uplink IPv6 routing table,
and a dedicated radvd instance.

### 2. Generate config files

```sh
uplinkmgr-setup
```

This writes:
- `/etc/dhcpcd.conf` — dhcpcd config covering all WAN and macvlan interfaces
  (backs up any existing file to `/etc/dhcpcd.conf.pre-uplinkmgr`)
- `/etc/network/interfaces.d/uplinkmgr.conf` — macvlan interface stanzas
- `/etc/uplinkmgr/radvd/radvd-uplinkmgr-<name>.conf` — radvd config per IPv6 uplink
- `/etc/systemd/system/radvd-uplinkmgr@.service` — parameterised radvd unit
- `/etc/iproute2/rt_tables.d/uplinkmgr.conf` — routing table name entries
- `/etc/uplinkmgr/uplinks/<name>.env` — per-uplink env files read by the hook

Run `uplinkmgr-setup --dry-run` to preview output without writing files.

### 3. Enable and start services

```sh
systemctl daemon-reload
systemctl restart dhcpcd
systemctl enable --now radvd-uplinkmgr@comcast.service   # repeat for each IPv6 uplink
systemctl enable --now uplinkmgr
```

Macvlan interfaces are brought up by ifupdown from the generated
`interfaces.d` stanza, so no additional step is needed for those.

### 4. NAT

uplinkmgr does not configure firewall rules. `uplinkmgr-setup` writes a
reference nftables masquerade fragment to
`/etc/uplinkmgr/uplinkmgr-nat.nft.example` as a starting point. Include or
adapt it in your site's nftables config.

---

## Route, address, and prefix management in detail

### IPv4 routing

uplinkmgr uses policy-based routing so that the kernel's main routing table
is left untouched (dhcpcd writes default routes there as a fallback; they
are used when the daemon is not running).

At startup the daemon installs two `ip rule` entries in the IPv4 rule table:

```
29000: from all lookup main suppress_prefixlength 0
29001: from all lookup uplinkmgr
```

Rule 29000 lets locally-originated traffic with a specific source route (a
/32 or narrower prefix in main) bypass the uplinkmgr table — this is how
`lo_to_uplink` rules work for outbound traffic from the router itself. Rule
29001 directs all other forwarded traffic to the `uplinkmgr` routing table
(table 160 by default).

The `uplinkmgr` table contains one `default` route per healthy uplink,
differentiated by metric:

```
default via 203.0.113.1 dev eth0 metric 100   # comcast (primary)
default via 198.51.100.1 dev eth1 metric 200  # starlink (backup)
```

The daemon installs these routes after reading the gateway from each
uplink's `.ipv4.state` file (written by the hook on `BOUND`/`RENEW`/`REBIND`).
When an uplink fails `failure_threshold` (default: 3) consecutive probes, its
route is removed from the table; all traffic silently shifts to the next
metric. When the uplink recovers `recovery_threshold` (default: 3)
consecutive probes, the route is reinstalled.

Per-uplink `lo_to_uplink` rules (priority 29001+N) ensure that packets
originating from the router itself and destined for an uplink's gateway (e.g.
for monitoring or DHCP) exit through the correct WAN interface.

### IPv6 addressing and macvlan interfaces

The core challenge with multi-homed IPv6 is getting clients to pick the right
source address when they have one global address per uplink. [RFC 6724 rule
5.5](https://www.rfc-editor.org/rfc/rfc6724#section-5) ("prefer outgoing
interface") resolves this: a client prefers a source address whose default
router is reachable via the same interface as the destination. This only works
if each uplink's prefix is associated with a *distinct router* — one with its
own MAC address and link-local — rather than being advertised by a single
shared router address. Macvlan interfaces provide exactly this: one virtual
router identity per uplink, all sharing the same physical LAN segment.

For each `(IPv6 uplink, network)` pair, uplinkmgr-setup creates a **macvlan**
interface parented on the network interface. A macvlan is a virtual NIC with
its own MAC address and link-local that shares the physical medium of its
parent. Each macvlan appears to LAN clients as an independent router, so
clients can form the router/prefix association that RFC 6724 rule 5.5 requires.

MAC addresses and link-locals are deterministic and stable:

| uplink index | network index | MAC                   | link-local    |
|:---:|:---:|---|---|
| 0 | 0 | `52:00:00:00:00:00` | `fe80::1:1`   |
| 1 | 0 | `52:01:00:00:00:00` | `fe80::1:2`   |
| 0 | 1 | `52:00:01:00:00:00` | `fe80::1:1`   |

Macvlan names follow the pattern `<net-iface>-u<uplink-index>` (truncated to
fit the 15-character kernel limit). Example: `vlan10-u0`, `vlan10-u1`.

### IPv6 prefix delegation

When `ipv6_pd: true`, dhcpcd sends a DHCPv6 Prefix Delegation request on the
WAN interface, requesting a hint prefix of `ipv6_pd_hint` bits (default `/56`).
The ISP returns a delegated prefix (e.g. `2001:db8:aaaa::/56`). dhcpcd
sub-delegates `/64` subnets to each macvlan using SLA IDs derived from the
network's position in the config file:

```
2001:db8:aaaa:0000::/64  →  vlan10-u0   (SLA ID 0 = first network)
2001:db8:aaaa:0001::/64  →  vlan20-u0   (SLA ID 1 = second network)
```

SLA IDs are fixed by config-file order and do not change if uplinks are
added or removed at the end of the list. Inserting or removing a network
from the middle of the list changes all subsequent SLA IDs; in that case
run `uplinkmgr-setup` again and restart dhcpcd.

The hook captures the delegated prefix from `$new_dhcp6_ia_pd1_prefix1` (and
its `_length`, `_vltime`, `_pltime` companions) and writes
`<uplink>.ipv6pd.state`. It also captures the upstream router's IPv6
link-local and RA lifetime from `ROUTERADVERT` events and writes
`<uplink>.ipv6ra.state`.

### IPv6 routing and policy rules

Each IPv6-capable uplink gets its own routing table (`uplinkmgr_comcast`,
`uplinkmgr_starlink`, …; table numbers 161, 162, … by default):

```
# table uplinkmgr_comcast (161):
default via fe80::1 dev eth0 expires 1800

# table uplinkmgr_starlink (162):
default via fe80::2 dev eth1 expires 1800
```

The daemon installs `ip -6 rule` entries that direct traffic arriving on a
macvlan to the corresponding uplink's table:

```
29001: iif vlan10-u0 lookup uplinkmgr_comcast
29002: iif vlan20-u0 lookup uplinkmgr_comcast
```

This enforces source–path consistency: a packet that a client sends to
`fe80::1:1` (the comcast macvlan's link-local) exits via `eth0` regardless
of the client's source address. A client sending to `fe80::1:2` (starlink
macvlan) exits via `eth1`.

A global suppression rule at the top of the IPv6 rule table prevents
the kernel's main table from overriding uplinkmgr's per-uplink routes for
forwarded traffic:

```
29000: from all lookup main suppress_prefixlength 0
```

### radvd and graceful prefix deprecation

One radvd instance runs per IPv6-capable uplink, reading its config from
`/etc/uplinkmgr/radvd/radvd-uplinkmgr-<name>.conf`. Each radvd advertises
the delegated prefix on its macvlan interfaces.

uplinkmgr controls the `AdvDefaultPreference` field in each RA:

| Uplink state | Rank among UP uplinks | Preference  |
|---|---|---|
| UP | first (lowest index) | `high`      |
| UP | any other            | `medium`    |
| DOWN | — | `low`       |

When an uplink transitions to DOWN, the daemon:

1. Rewrites the radvd config with `AdvDefaultPreference low`,
   `AdvPreferredLifetime 0`, `AdvValidLifetime 0`, and
   `DecrementLifetimes off`. Setting the valid lifetime to 0 immediately
   invalidates the prefix. `DecrementLifetimes off` is required so that
   radvd continues sending RAs with a zero valid lifetime rather than
   silently stopping when the lifetime hits 0.
2. Sends `SIGHUP` to the radvd instance so it re-reads the config.

Clients with addresses from the failed uplink's prefix can finish existing
connections. New connections automatically use a prefix from a healthy
uplink because RFC 6724 prefers addresses with a non-zero preferred lifetime
and a `high`/`medium`-preference default router.

On recovery, the daemon recalculates preference tiers for all IPv6 uplinks
(recovery of one uplink can demote others from `high` to `medium`) and
rewrites and re-SIGHUPs all radvd instances.

### Daemon stop / failsafe

When the daemon stops (SIGTERM), it removes all routes and rules it installed
from the kernel, then rewrites all radvd configs in the optimistic state (all
uplinks UP with priority-ordered preferences) and sends SIGHUP to each radvd.
Traffic falls back to dhcpcd's main-table default routes (which dhcpcd
maintains independently). Connectivity is preserved without the daemon.

### Monitoring

The daemon probes each uplink every `monitor.interval` seconds (default: 10s)
by pinging each host in `monitor.v4_hosts` (default: `8.8.8.8`, `1.1.1.1`)
with `-I <wan-iface>` to force use of that uplink. A probe passes if any
host responds. IPv6 uplinks are probed similarly via `monitor.v6_hosts`.

Probes for different uplinks run in parallel (one thread per uplink), so a
slow uplink does not delay detection of a failure on another.

State transitions use hysteresis:

- **UP → DOWN**: after `failure_threshold` consecutive failed probes (default: 3)
- **DOWN → UP**: after `recovery_threshold` consecutive successful probes (default: 3)

With default settings, failover takes 30 seconds (3 × 10s interval) in the
worst case. Adjust `interval` and `failure_threshold` to trade off detection
latency against sensitivity to transient packet loss.

---

## Configuration reference

```yaml
uplinkmgr:
  # Routing table numbers: table routing_table_start = shared IPv4 table;
  # tables routing_table_start+1 .. +N = per-uplink IPv6 tables.
  routing_table_start: 160       # default

  # Base priority for ip rule entries (must not conflict with other rules).
  rule_priority_start: 29000     # default

  # If true, prohibit macvlan traffic whose source is from a different uplink's PD prefix.
  reject_wrong_pd_src: false    # default

  # Minimum seconds between radvd restarts (lifetime refresh rate limit).
  radvd_min_restart_interval: 60 # default

  monitor:
    interval: 10                 # probe cycle period in seconds
    failure_threshold: 3         # consecutive failures to declare DOWN
    recovery_threshold: 3        # consecutive successes to declare UP
    ping_count: 3                # pings per host per probe
    v4_hosts: [8.8.8.8, 1.1.1.1]
    v6_hosts: [2001:4860:4860::8888, 2606:4700:4700::1111]

  networks:
    - name: lan                  # logical name (used in logs)
      interface: vlan10          # LAN-side interface

  uplinks:
    - name: comcast              # logical name; alphanumeric + hyphens
      interface: eth0            # WAN interface
      metric: 100                # IPv4 route metric; lower = preferred
      ipv6_pd: true              # request DHCPv6 prefix delegation
      ipv6_pd_hint: 56           # requested prefix length hint (bits)
      ia_na: false               # also request a DHCPv6 IA_NA address
```

## Requirements

- Debian 13 (Trixie) or equivalent
- dhcpcd 10.x (`dhcpcd5` package)
- radvd 2.x (`radvd` package)
- iproute2, iputils-ping, ifupdown
- Python 3.9+, python3-yaml

## Known limitations

- Only DHCP WAN connections are supported (no PPPoE or static WAN).
- Only one delegated prefix per uplink is used; multiple ISP-provided
  prefixes are not supported.
- Uplink index order in the config file is stable: removing an uplink from
  the middle of the list renumbers all subsequent uplinks' macvlan names,
  MACs, link-locals, and routing table numbers, requiring a full
  `uplinkmgr-setup` re-run and service restart.
