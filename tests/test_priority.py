"""Tests for priority.py — ip rule priority allocation."""

import pytest
from uplinkmgr import priority
from tests.conftest import make_config, make_uplink, make_network


@pytest.mark.parametrize("n_uplinks,n_networks", [
    (1, 1),
    (1, 2),
    (2, 1),
    (2, 2),
    (3, 3),
])
def test_ipv4_priority_ordering_and_values(n_uplinks, n_networks):
    cfg = make_config(
        networks=[make_network(f"net{i}", f"eth{i+10}") for i in range(n_networks)],
        uplinks=[make_uplink(f"isp{i}", f"wan{i}", index=i) for i in range(n_uplinks)],
    )
    base = cfg.rule_priority_start
    internal = priority.ipv4_internal_traffic_priority(cfg)
    lo_prios = [priority.ipv4_lo_to_uplink_priority(cfg, i) for i in range(n_uplinks)]
    fwd = priority.ipv4_fwd_to_wan_priority(cfg)

    assert internal == base
    assert lo_prios[0] == base + 1
    for i in range(1, n_uplinks):
        assert lo_prios[i] == lo_prios[i - 1] + 1
    assert fwd == base + 1 + n_uplinks

    all_prios = [internal] + lo_prios + [fwd]
    assert all_prios == sorted(all_prios)
    assert len(set(all_prios)) == len(all_prios)


@pytest.mark.parametrize("n_uplinks,n_networks", [
    (1, 1),
    (1, 2),
    (2, 1),
    (2, 2),
    (3, 3),
])
def test_ipv6_priority_ordering_and_uniqueness(n_uplinks, n_networks):
    cfg = make_config(
        networks=[make_network(f"net{i}", f"eth{i+10}") for i in range(n_networks)],
        uplinks=[make_uplink(f"isp{i}", f"wan{i}", index=i) for i in range(n_uplinks)],
    )
    base = cfg.rule_priority_start
    internal = priority.ipv6_internal_traffic_priority(cfg)
    fwd_prios = [
        priority.ipv6_fwd_to_uplink_priority(cfg, ui, ni)
        for ui in range(n_uplinks)
        for ni in range(n_networks)
    ]
    lo_prios = [priority.ipv6_lo_to_uplink_priority(cfg, i) for i in range(n_uplinks)]
    prohibit_prios = [
        priority.ipv6_reject_wrong_pd_src_priority(cfg, ui, ni)
        for ui in range(n_uplinks)
        for ni in range(n_networks)
    ]
    pd_lo_prios = [
        priority.ipv6_pd_lo_to_uplink_priority(cfg, i) for i in range(n_uplinks)
    ]

    assert internal == base

    all_prios = [internal] + fwd_prios + lo_prios + prohibit_prios + pd_lo_prios
    assert all_prios == sorted(all_prios), "IPv6 priorities not strictly ordered"
    assert len(set(all_prios)) == len(all_prios), "IPv6 priorities not unique"


def test_ipv4_and_ipv6_internal_start_at_same_base():
    # Both protocol namespaces start at rule_priority_start (they don't conflict
    # because `ip rule` and `ip -6 rule` are separate tables).
    cfg = make_config()
    assert priority.ipv4_internal_traffic_priority(cfg) == cfg.rule_priority_start
    assert priority.ipv6_internal_traffic_priority(cfg) == cfg.rule_priority_start
