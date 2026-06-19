"""Tests for generator.py — config file content generation."""

import pytest
from tests.conftest import make_config, make_uplink, make_network
from uplinkmgr.generator import (
    HEADER,
    dhcpcd_conf,
    env_file,
    interfaces_file,
    radvd_conf_from_state,
    radvd_template_unit,
    rt_tables,
)


# --- dhcpcd_conf ---

def test_dhcpcd_no_ipv6_no_stanza():
    cfg = make_config(uplinks=[make_uplink(ipv6_pd=False, ia_na=False)])
    out = dhcpcd_conf(cfg)
    assert "ipv6rs" not in out
    assert "ia_na" not in out
    assert "ia_pd" not in out
    assert "duid" not in out


def test_dhcpcd_ipv6pd_only_iaid_1():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True, ia_na=False)],
    )
    out = dhcpcd_conf(cfg)
    assert "ipv6rs" in out
    assert "ia_na" not in out
    assert "    ia_pd 1/" in out  # iaid=1 since no ia_na
    assert "duid" in out


def test_dhcpcd_ia_na_only_iaid_1():
    cfg = make_config(uplinks=[make_uplink(ia_na=True, ipv6_pd=False)])
    out = dhcpcd_conf(cfg)
    assert "ipv6rs" in out
    assert "    ia_na 1\n" in out
    assert "ia_pd" not in out
    assert "duid" in out


def test_dhcpcd_ia_na_and_ipv6pd_sequential_iaids():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ia_na=True, ipv6_pd=True)],
    )
    out = dhcpcd_conf(cfg)
    assert "    ia_na 1\n" in out
    assert "    ia_pd 2/" in out  # iaid=2 since ia_na took 1


def test_dhcpcd_header_always_first():
    cfg = make_config()
    assert dhcpcd_conf(cfg).startswith(HEADER)
    assert dhcpcd_conf(cfg, head="# admin\n").startswith(HEADER)
    assert dhcpcd_conf(cfg, tail="# end\n").startswith(HEADER)


def test_dhcpcd_head_placed_after_header():
    cfg = make_config()
    head = "# custom global option\n"
    out = dhcpcd_conf(cfg, head=head)
    header_pos = out.index(HEADER)
    head_pos = out.index("# custom global option")
    assert header_pos < head_pos


def test_dhcpcd_tail_at_end():
    cfg = make_config()
    out = dhcpcd_conf(cfg, tail="# tail content\n")
    assert out.endswith("# tail content\n")


def test_dhcpcd_head_without_trailing_newline():
    cfg = make_config()
    out = dhcpcd_conf(cfg, head="# no newline")
    assert "# no newline" in out


def test_dhcpcd_allowinterfaces_includes_wan_and_macvlan():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = dhcpcd_conf(cfg)
    assert "allowinterfaces eth0 eth1-u0" in out


def test_dhcpcd_allowinterfaces_wan_only_when_no_pd():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=False)],
    )
    out = dhcpcd_conf(cfg)
    assert "allowinterfaces eth0\n" in out
    assert "eth1-u0" not in out


# --- radvd_template_unit ---

def test_radvd_template_unit_uses_percent_i():
    out = radvd_template_unit()
    assert "%i" in out


def test_radvd_template_unit_no_literal_uplink_name():
    out = radvd_template_unit()
    for name in ("comcast", "isp", "provider"):
        assert name not in out


def test_radvd_template_unit_has_two_execstart_lines():
    out = radvd_template_unit()
    assert out.count("ExecStart=") == 2


def test_radvd_template_unit_conf_path():
    out = radvd_template_unit()
    assert "/etc/uplinkmgr/radvd/radvd-uplinkmgr-%i.conf" in out


def test_radvd_template_unit_pid_path():
    out = radvd_template_unit()
    assert "/run/radvd-uplinkmgr-%i.pid" in out


# --- radvd_conf_from_state ---

def test_radvd_conf_from_state_is_down_zeroes_preferred_lifetime():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = radvd_conf_from_state(
        cfg, cfg.uplinks[0],
        preference="low",
        default_lifetime=0,
        route_lifetime=0,
        per_iface_prefixes={"eth1-u0": "2001:db8:1::/64"},
        valid_lifetime=7200,
        preferred_lifetime=1800,
        is_down=True,
    )
    assert "AdvPreferredLifetime 0;" in out
    assert "AdvValidLifetime 0;" in out
    assert "DecrementLifetimes off;" in out


def test_radvd_conf_from_state_is_up_passes_through_lifetimes():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = radvd_conf_from_state(
        cfg, cfg.uplinks[0],
        preference="high",
        default_lifetime=1800,
        route_lifetime=1800,
        per_iface_prefixes={"eth1-u0": "2001:db8:1::/64"},
        valid_lifetime=7200,
        preferred_lifetime=3600,
        is_down=False,
    )
    assert "AdvPreferredLifetime 3600;" in out
    assert "AdvValidLifetime 7200;" in out


def test_radvd_conf_from_state_prefix_in_output():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = radvd_conf_from_state(
        cfg, cfg.uplinks[0],
        preference="high",
        default_lifetime=1800,
        route_lifetime=1800,
        per_iface_prefixes={"eth1-u0": "2001:db8:abcd::/64"},
        valid_lifetime=7200,
        preferred_lifetime=3600,
        is_down=False,
    )
    assert "2001:db8:abcd::/64" in out


def test_radvd_conf_from_state_missing_prefix_falls_back():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = radvd_conf_from_state(
        cfg, cfg.uplinks[0],
        preference="high",
        default_lifetime=1800,
        route_lifetime=1800,
        per_iface_prefixes={},  # no entry for eth1-u0
        valid_lifetime=7200,
        preferred_lifetime=3600,
        is_down=False,
    )
    assert "::/64" in out


# --- rt_tables ---

def test_rt_tables_contains_ipv4_and_ipv6_tables():
    cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0)])
    out = rt_tables(cfg)
    assert "160\tuplinkmgr\n" in out
    assert "161\tuplinkmgr_isp\n" in out


def test_rt_tables_multiple_uplinks():
    cfg = make_config(
        uplinks=[
            make_uplink("isp1", "eth0", index=0),
            make_uplink("isp2", "eth3", index=1),
        ],
    )
    out = rt_tables(cfg)
    assert "160\tuplinkmgr\n" in out
    assert "161\tuplinkmgr_isp1\n" in out
    assert "162\tuplinkmgr_isp2\n" in out


# --- env_file ---

def test_env_file_no_ipv6():
    cfg = make_config(uplinks=[make_uplink("isp", "eth0", ipv6_pd=False, ia_na=False)])
    out = env_file(cfg, cfg.uplinks[0])
    assert "UPLINKMGR_UPLINK_NAME=isp\n" in out
    assert "UPLINKMGR_WAN_IFACE=eth0\n" in out
    assert "UPLINKMGR_IPV6_PD=false\n" in out
    assert "UPLINKMGR_IPV6_IA_NA=false\n" in out


def test_env_file_with_ipv6_pd_and_ia_na():
    cfg = make_config(uplinks=[make_uplink("isp", "eth0", ipv6_pd=True, ia_na=True)])
    out = env_file(cfg, cfg.uplinks[0])
    assert "UPLINKMGR_IPV6_PD=true\n" in out
    assert "UPLINKMGR_IPV6_IA_NA=true\n" in out


# --- interfaces_file ---

def test_interfaces_file_includes_macvlan_for_pd_uplink():
    cfg = make_config(
        networks=[make_network("lan", "eth1")],
        uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
    )
    out = interfaces_file(cfg)
    assert "auto eth1-u0" in out
    assert "iface eth1-u0 inet manual" in out
    assert "type macvlan" in out


def test_interfaces_file_empty_for_no_pd_uplinks():
    cfg = make_config(uplinks=[make_uplink(ipv6_pd=False)])
    out = interfaces_file(cfg)
    # Only the header; no interface stanzas
    assert "auto " not in out
    assert "iface " not in out
