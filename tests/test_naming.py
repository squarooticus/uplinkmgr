"""Tests for naming.py — interface names, table names, path helpers."""

import pytest
from uplinkmgr.config import NetworkConfig
from uplinkmgr.naming import (
    macvlan_name, mac_address, macvlan_iaid, link_local,
    ipv4_table_num, ipv4_table_name,
    ipv6_table_num, ipv6_table_name,
    radvd_conf_path, radvd_unit_name,
    env_file_path, env_symlink_path,
    validate_macvlan_names,
)
from tests.conftest import make_config, make_uplink, make_network


# --- macvlan_name ---

def test_macvlan_name_short_no_truncation():
    assert macvlan_name("eth1", 0) == "eth1-u0"


def test_macvlan_name_truncated_to_15():
    name = macvlan_name("averylongiface", 0)
    assert len(name) == 15
    assert name.endswith("-u0")


def test_macvlan_name_dot_replaced_with_underscore():
    # "eth0.20": dot replaced with "_" -> "eth0_20"; numeric suffix "20" (2 chars),
    # uplink suffix "-u1" (3 chars); available for alpha prefix = 15-2-3=10; "eth0_" fits
    name = macvlan_name("eth0.20", 1)
    assert name == "eth0_20-u1"
    assert "." not in name
    assert len(name) <= 15


def test_macvlan_name_multiple_dots_all_replaced():
    name = macvlan_name("sfp0.1.20", 1)
    assert "." not in name
    assert name == "sfp0_1_20-u1"
    assert len(name) <= 15


def test_macvlan_name_exact_15_chars():
    # "eth1" (4 chars) + "-u0" (3 chars) = 7 chars, well under 15
    name = macvlan_name("eth1", 0)
    assert name == "eth1-u0"


def test_macvlan_name_numeric_suffix_overflow_raises():
    # "1234567890123" (13-digit numeric suffix) + "-u0" (3 chars) = 16 > 15
    with pytest.raises(ValueError):
        macvlan_name("1234567890123", 0)


def test_macvlan_name_collision_detected():
    # Two 15-char interface names differing only in the last char; both truncate
    # to the same 12-char alpha prefix → same macvlan name for the same uplink.
    cfg = make_config(
        networks=[
            NetworkConfig("n1", "averylongifacex"),
            NetworkConfig("n2", "averylongifacey"),
        ],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    with pytest.raises(ValueError, match="collision"):
        validate_macvlan_names(cfg)


def test_validate_macvlan_names_no_collision(dual_uplink_cfg):
    validate_macvlan_names(dual_uplink_cfg)  # must not raise


def test_validate_macvlan_names_no_pd_uplinks():
    cfg = make_config(uplinks=[make_uplink(ipv6_pd=False)])
    validate_macvlan_names(cfg)  # no macvlans to check; must not raise


# --- mac_address ---

def test_mac_address_zeros():
    assert mac_address(0, 0) == "52:00:00:00:00:00"


def test_mac_address_non_zero():
    assert mac_address(1, 2) == "52:01:02:00:00:00"


def test_mac_address_max():
    assert mac_address(255, 255) == "52:ff:ff:00:00:00"


# --- macvlan_iaid ---

def test_macvlan_iaid_zeros():
    assert macvlan_iaid(0, 0) == 0x1000


def test_macvlan_iaid_non_zero():
    assert macvlan_iaid(1, 2) == 0x1000 + (1 << 8) + 2


def test_macvlan_iaid_unique_across_uplinks_same_network():
    # Same net_idx (same network), different uplinks -> must differ.
    assert macvlan_iaid(0, 0) != macvlan_iaid(1, 0)


def test_macvlan_iaid_unique_across_networks_same_uplink():
    assert macvlan_iaid(0, 0) != macvlan_iaid(0, 1)


# --- link_local ---

def test_link_local_zero():
    assert link_local(0) == "fe80::1:1"


def test_link_local_nonzero():
    assert link_local(3) == "fe80::1:4"


# --- table numbers ---

def test_ipv4_table_num():
    assert ipv4_table_num(160) == 160
    assert ipv4_table_num(100) == 100


def test_ipv4_table_name():
    assert ipv4_table_name() == "uplinkmgr"


def test_ipv6_table_num_offset():
    assert ipv6_table_num(160, 0) == 161
    assert ipv6_table_num(160, 1) == 162
    assert ipv6_table_num(100, 3) == 104


def test_ipv6_table_name():
    assert ipv6_table_name("comcast") == "uplinkmgr_comcast"
    assert ipv6_table_name("isp-1") == "uplinkmgr_isp-1"


# --- path/name helpers ---

def test_radvd_conf_path():
    assert radvd_conf_path("comcast") == "/etc/uplinkmgr/radvd/radvd-uplinkmgr-comcast.conf"


def test_radvd_unit_name():
    assert radvd_unit_name("comcast") == "radvd-uplinkmgr@comcast.service"


def test_env_file_path():
    assert env_file_path("comcast") == "/etc/uplinkmgr/uplinks/comcast.env"


def test_env_symlink_path():
    assert env_symlink_path("eth0") == "/etc/uplinkmgr/uplinks/eth0.env"
