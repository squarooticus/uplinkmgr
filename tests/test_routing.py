"""Tests for routing.py — ip command construction with mocked subprocess."""

import logging
from unittest.mock import patch, MagicMock
from uplinkmgr import routing


def _result(returncode: int, stderr: bytes = b"") -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    r.stderr = stderr
    return r


OK = _result(0)
FAIL = _result(1, b"RTNETLINK answers: File exists")


def _cmd(mock) -> list:
    return mock.call_args[0][0]


def _cmds(mock) -> list[list]:
    return [c[0][0] for c in mock.call_args_list]


# --- IPv4 routes ---

def test_replace_ipv4_route():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.replace_ipv4_route("192.168.1.1", "eth0", 100, 160)
    assert _cmd(m) == [
        "ip", "route", "replace", "default",
        "via", "192.168.1.1", "dev", "eth0",
        "metric", "100", "table", "160",
    ]


def test_del_ipv4_route():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.del_ipv4_route("eth0", 160)
    assert _cmd(m) == ["ip", "route", "del", "default", "dev", "eth0", "table", "160"]


# --- IPv6 routes ---

def test_replace_ipv6_route_with_expiry():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.replace_ipv6_route("fe80::1", "eth0", 161, lifetime=3600, remaining=1800)
    cmd = _cmd(m)
    assert cmd[:6] == ["ip", "-6", "route", "replace", "default", "via"]
    assert "expires" in cmd
    assert cmd[cmd.index("expires") + 1] == "1800"


def test_replace_ipv6_route_infinite_no_expires():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.replace_ipv6_route("fe80::1", "eth0", 161, lifetime=0, remaining=0)
    assert "expires" not in _cmd(m)


def test_del_ipv6_route():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.del_ipv6_route("eth0", 161)
    assert _cmd(m) == ["ip", "-6", "route", "del", "default", "dev", "eth0", "table", "161"]


# --- Global policy rules ---

def test_add_ipv4_policy_rules():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv4_policy_rules(
            internal_traffic_priority=29000,
            fwd_to_wan_priority=29003,
            ipv4_table=160,
        )
    cmds = _cmds(m)
    assert len(cmds) == 2
    assert cmds[0] == [
        "ip", "rule", "add",
        "lookup", "main", "suppress_prefixlength", "0",
        "priority", "29000",
    ]
    assert cmds[1] == ["ip", "rule", "add", "lookup", "160", "priority", "29003"]


def test_del_ipv4_policy_rules():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.del_ipv4_policy_rules(
            internal_traffic_priority=29000,
            fwd_to_wan_priority=29003,
        )
    cmds = _cmds(m)
    assert len(cmds) == 2
    assert cmds[0] == ["ip", "rule", "del", "priority", "29000"]
    assert cmds[1] == ["ip", "rule", "del", "priority", "29003"]


def test_add_ipv6_policy_rule():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv6_policy_rule(internal_traffic_priority=29000)
    assert _cmd(m) == [
        "ip", "-6", "rule", "add",
        "lookup", "main", "suppress_prefixlength", "0",
        "priority", "29000",
    ]


def test_del_ipv6_policy_rule():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.del_ipv6_policy_rule(internal_traffic_priority=29000)
    assert _cmd(m) == ["ip", "-6", "rule", "del", "priority", "29000"]


# --- IPv6 rules ---

def test_add_ipv6_lo_to_uplink_rule():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv6_lo_to_uplink_rule("2001:db8::/64", 161, 29005)
    assert _cmd(m) == [
        "ip", "-6", "rule", "add",
        "from", "2001:db8::/64", "iif", "lo",
        "lookup", "161", "priority", "29005",
    ]


def test_add_ipv6_fwd_to_uplink_rule_without_prefix():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv6_fwd_to_uplink_rule("eth1-u0", 161, 29001, prefix=None)
    assert _cmd(m) == [
        "ip", "-6", "rule", "add",
        "iif", "eth1-u0", "lookup", "161", "priority", "29001",
    ]


def test_add_ipv6_fwd_to_uplink_rule_with_prefix():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv6_fwd_to_uplink_rule("eth1-u0", 161, 29001, prefix="2001:db8::/56")
    cmd = _cmd(m)
    assert "from" in cmd
    assert cmd[cmd.index("from") + 1] == "2001:db8::/56"


# --- IPv4 per-uplink rule ---

def test_add_ipv4_lo_to_uplink_rule():
    with patch("uplinkmgr.routing.subprocess.run", return_value=OK) as m:
        routing.add_ipv4_lo_to_uplink_rule("10.0.0.5", 161, 29002)
    assert _cmd(m) == [
        "ip", "rule", "add",
        "from", "10.0.0.5",
        "lookup", "161", "priority", "29002",
    ]


# --- Failure handling ---

def test_routing_failure_does_not_raise(caplog):
    with patch("uplinkmgr.routing.subprocess.run", return_value=FAIL):
        with caplog.at_level(logging.ERROR, logger="uplinkmgr.routing"):
            routing.replace_ipv4_route("192.168.1.1", "eth0", 100, 160)
    assert any("command failed" in r.message for r in caplog.records)


def test_del_failure_logs_warning_not_error(caplog):
    with patch("uplinkmgr.routing.subprocess.run", return_value=FAIL):
        with caplog.at_level(logging.WARNING, logger="uplinkmgr.routing"):
            routing.del_ipv4_route("eth0", 160)
    assert any("delete command failed" in r.message for r in caplog.records)
