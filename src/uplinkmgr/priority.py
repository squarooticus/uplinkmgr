"""ip rule priority allocation for uplinkmgr."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# IPv4 rule priorities  (ip rule — separate namespace from IPv6)
#
# rule_priority_start + 0              ipv4_internal_traffic_priority  (global)
# rule_priority_start + 1 + idx        ipv4_lo_to_uplink_priority      (per uplink)
# rule_priority_start + 1 + n_uplinks  ipv4_fwd_to_wan_priority        (global)
# ---------------------------------------------------------------------------

def ipv4_internal_traffic_priority(cfg: "Config") -> int:
    return cfg.rule_priority_start


def ipv4_lo_to_uplink_priority(cfg: "Config", uplink_idx: int) -> int:
    return cfg.rule_priority_start + 1 + uplink_idx


def ipv4_fwd_to_wan_priority(cfg: "Config") -> int:
    return cfg.rule_priority_start + 1 + len(cfg.uplinks)


# ---------------------------------------------------------------------------
# IPv6 rule priorities  (ip -6 rule — separate namespace from IPv4)
#
# Let N = n_uplinks * n_networks, M = n_networks.
#
# rule_priority_start + 0                        ipv6_internal_traffic_priority  (global)
# rule_priority_start + 1 + uplink*M + net       ipv6_fwd_to_uplink_priority
# rule_priority_start + 1 + N + idx              ipv6_lo_to_uplink_priority
# rule_priority_start + 1 + N + n_uplinks + ..   ipv6_reject_wrong_pd_src_priority
# ---------------------------------------------------------------------------

def ipv6_internal_traffic_priority(cfg: "Config") -> int:
    return cfg.rule_priority_start


def ipv6_fwd_to_uplink_priority(cfg: "Config", uplink_idx: int, net_idx: int) -> int:
    return cfg.rule_priority_start + 1 + uplink_idx * len(cfg.networks) + net_idx


def ipv6_lo_to_uplink_priority(cfg: "Config", uplink_idx: int) -> int:
    N = len(cfg.uplinks) * len(cfg.networks)
    return cfg.rule_priority_start + 1 + N + uplink_idx


def ipv6_reject_wrong_pd_src_priority(cfg: "Config", uplink_idx: int, net_idx: int) -> int:
    N = len(cfg.uplinks) * len(cfg.networks)
    return (cfg.rule_priority_start + 1 + N + len(cfg.uplinks)
            + uplink_idx * len(cfg.networks) + net_idx)
