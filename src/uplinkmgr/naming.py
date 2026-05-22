"""Naming convention utilities shared by setup and daemon."""

from __future__ import annotations

from .config import Config, UplinkConfig, NetworkConfig


def macvlan_name(net_iface: str, uplink_idx: int) -> str:
    """Return the macvlan interface name for a (network, uplink) pair.

    Truncates the network interface prefix if the full name would exceed
    15 characters (Linux IFNAMSIZ - 1).
    """
    suffix = f"-u{uplink_idx}"
    max_prefix = 15 - len(suffix)
    prefix = net_iface[:max_prefix]
    return f"{prefix}{suffix}"


def mac_address(uplink_idx: int, net_idx: int) -> str:
    """Return the MAC address for a macvlan interface.

    Format: 52:<uplink_idx>:<net_idx>:00:00:00
    """
    return f"52:{uplink_idx:02x}:{net_idx:02x}:00:00:00"


def link_local(uplink_idx: int) -> str:
    """Return the link-local address for all macvlan interfaces of an uplink."""
    return f"fe80::1:{uplink_idx}"


def table_num(routing_table_start: int, uplink_idx: int) -> int:
    return routing_table_start + uplink_idx


def table_name(uplink_name: str) -> str:
    return f"uplinkmgr_{uplink_name}"


def rule_priority(rule_priority_start: int, uplink_idx: int, net_idx: int,
                  num_networks: int) -> int:
    """Return the ip -6 rule priority for a macvlan interface.

    Priorities are globally sequential across all (uplink, network) pairs:
    uplink N starts at rule_priority_start + N * num_networks.
    """
    return rule_priority_start + uplink_idx * num_networks + net_idx


def dhcpcd_conf_path(uplink_name: str) -> str:
    return f"/etc/dhcpcd-uplinkmgr-{uplink_name}.conf"


def dhcpcd_unit_name(uplink_name: str) -> str:
    return f"dhcpcd-uplinkmgr-{uplink_name}.service"


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
