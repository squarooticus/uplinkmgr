"""Naming convention utilities shared by setup and daemon."""

from __future__ import annotations

import re

from .config import Config, UplinkConfig, NetworkConfig

_NUMERIC_SUFFIX_RE = re.compile(r'[0-9.]*[0-9]$')


def macvlan_name(net_iface: str, uplink_idx: int) -> str:
    """Return the macvlan interface name for a (network, uplink) pair.

    If net_iface ends with a numeric/dot suffix (e.g. "1.20"), that suffix
    is preserved intact; only the leading alpha portion is truncated.
    Raises ValueError if the numeric suffix alone already leaves no room.
    """
    suffix = f"-u{uplink_idx}"
    m = _NUMERIC_SUFFIX_RE.search(net_iface)
    if m:
        numeric_suffix = m.group()
        alpha_prefix = net_iface[:m.start()]
        available = 15 - len(numeric_suffix) - len(suffix)
        if available < 0:
            raise ValueError(
                f"cannot generate macvlan name for interface '{net_iface}' "
                f"uplink {uplink_idx}: numeric suffix '{numeric_suffix}' + "
                f"'{suffix}' ({len(numeric_suffix) + len(suffix)} chars) "
                f"already exceeds 15-character limit"
            )
        return f"{alpha_prefix[:available]}{numeric_suffix}{suffix}"
    else:
        max_prefix = 15 - len(suffix)
        return f"{net_iface[:max_prefix]}{suffix}"


def mac_address(uplink_idx: int, net_idx: int) -> str:
    """Return the MAC address for a macvlan interface.

    Format: 52:<uplink_idx>:<net_idx>:00:00:00
    """
    return f"52:{uplink_idx:02x}:{net_idx:02x}:00:00:00"


def link_local(uplink_idx: int) -> str:
    """Return the link-local address for all macvlan interfaces of an uplink."""
    return f"fe80::1:{uplink_idx}"


def ipv4_table_num(routing_table_start: int) -> int:
    """Shared IPv4 uplinkmgr routing table number."""
    return routing_table_start


def ipv4_table_name() -> str:
    return "uplinkmgr"


def table_num(routing_table_start: int, uplink_idx: int) -> int:
    """IPv6 per-uplink routing table number."""
    return routing_table_start + 1 + uplink_idx


def table_name(uplink_name: str) -> str:
    return f"uplinkmgr_{uplink_name}"


def dhcpcd_unit_name(uplink_name: str) -> str:
    return f"dhcpcd-uplinkmgr-{uplink_name}.service"


# ---------------------------------------------------------------------------
# ip rule priority helpers — all take Config to derive N = uplinks * networks
# ---------------------------------------------------------------------------

def internal_traffic_priority(cfg: "Config", uplink_idx: int, net_idx: int) -> int:
    return cfg.rule_priority_start + uplink_idx * len(cfg.networks) + net_idx


def fwd_to_uplink_priority(cfg: "Config", uplink_idx: int, net_idx: int) -> int:
    N = len(cfg.uplinks) * len(cfg.networks)
    return cfg.rule_priority_start + N + uplink_idx * len(cfg.networks) + net_idx


def lo_to_uplink_priority(cfg: "Config", uplink_idx: int) -> int:
    N = len(cfg.uplinks) * len(cfg.networks)
    return cfg.rule_priority_start + 2 * N + uplink_idx


def prohibit_wrong_src_priority(cfg: "Config", uplink_idx: int, net_idx: int) -> int:
    N = len(cfg.uplinks) * len(cfg.networks)
    return cfg.rule_priority_start + 2 * N + len(cfg.uplinks) + uplink_idx * len(cfg.networks) + net_idx


def ipv4_suppress_priority(cfg: "Config") -> int:
    return cfg.rule_priority_start


def ipv4_lookup_priority(cfg: "Config") -> int:
    return cfg.rule_priority_start + 1



def radvd_conf_path(uplink_name: str) -> str:
    return f"/etc/radvd/radvd-uplinkmgr-{uplink_name}.conf"


def radvd_unit_name(uplink_name: str) -> str:
    return f"radvd-uplinkmgr-{uplink_name}.service"


def env_file_path(uplink_name: str) -> str:
    return f"/etc/uplinkmgr/uplinks/{uplink_name}.env"


def env_symlink_path(iface: str) -> str:
    return f"/etc/uplinkmgr/uplinks/{iface}.env"


def macvlan_pairs(cfg: Config) -> list[tuple[UplinkConfig, NetworkConfig, str]]:
    """Return all (uplink, network, macvlan_name) triples for IPv6-PD uplinks."""
    result = []
    for uplink in cfg.uplinks:
        if not uplink.ipv6_pd:
            continue
        for net in cfg.networks:
            mv_name = macvlan_name(net.interface, uplink.index)
            result.append((uplink, net, mv_name))
    return result


def validate_macvlan_names(cfg: Config) -> None:
    """Raise ValueError if any two macvlan names would collide after truncation."""
    seen: dict[str, str] = {}
    for uplink in cfg.uplinks:
        if not uplink.ipv6_pd:
            continue
        for net in cfg.networks:
            mv = macvlan_name(net.interface, uplink.index)
            key = mv
            desc = f"uplink '{uplink.name}' + network '{net.name}'"
            if key in seen:
                raise ValueError(
                    f"macvlan name collision: '{mv}' produced by both "
                    f"{seen[key]} and {desc}"
                )
            seen[key] = desc
