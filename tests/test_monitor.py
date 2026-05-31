"""Tests for monitor.py — uplink probe logic with mocked subprocess."""

from unittest.mock import patch, MagicMock, call
from uplinkmgr import monitor


def _result(returncode: int) -> MagicMock:
    r = MagicMock()
    r.returncode = returncode
    return r


OK = _result(0)
FAIL = _result(1)


# --- probe_ipv4 ---

def test_probe_ipv4_first_attempt_succeeds():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=OK) as m:
        result = monitor.probe_ipv4("eth0", ["8.8.8.8", "1.1.1.1"], count=3)
    assert result is True
    assert m.call_count == 1  # short-circuits on first success


def test_probe_ipv4_all_fail():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=FAIL) as m:
        result = monitor.probe_ipv4("eth0", ["8.8.8.8", "1.1.1.1"], count=3)
    assert result is False
    assert m.call_count == 6  # 2 hosts × 3 attempts


def test_probe_ipv4_succeeds_on_third_attempt_of_first_host():
    side_effects = [FAIL, FAIL, OK]
    with patch("uplinkmgr.monitor.subprocess.run", side_effect=side_effects) as m:
        result = monitor.probe_ipv4("eth0", ["8.8.8.8", "1.1.1.1"], count=3)
    assert result is True
    assert m.call_count == 3  # 3 attempts to first host; never reaches second


def test_probe_ipv4_correct_command():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=OK) as m:
        monitor.probe_ipv4("eth0", ["8.8.8.8"], count=1)
    cmd = m.call_args[0][0]
    assert cmd == ["ping", "-c", "1", "-W", "2", "-n", "-q", "-I", "eth0", "8.8.8.8"]


def test_probe_ipv4_count_one_single_call_per_host():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=FAIL) as m:
        monitor.probe_ipv4("eth0", ["8.8.8.8", "1.1.1.1"], count=1)
    assert m.call_count == 2  # 2 hosts × 1 attempt


# --- probe_ipv6 ---

def test_probe_ipv6_uses_ping6():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=OK) as m:
        monitor.probe_ipv6("eth0", ["2001:4860:4860::8888"], count=1)
    cmd = m.call_args[0][0]
    assert cmd[0] == "ping6"


def test_probe_ipv6_correct_command():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=OK) as m:
        monitor.probe_ipv6("eth0", ["2001:4860:4860::8888"], count=1)
    cmd = m.call_args[0][0]
    assert cmd == [
        "ping6", "-c", "1", "-W", "2", "-n", "-q",
        "-I", "eth0", "2001:4860:4860::8888",
    ]


def test_probe_ipv6_all_fail():
    with patch("uplinkmgr.monitor.subprocess.run", return_value=FAIL) as m:
        result = monitor.probe_ipv6("eth0", ["2001:4860:4860::8888"], count=2)
    assert result is False
    assert m.call_count == 2


# --- ipv6_default_route_exists ---

def test_ipv6_default_route_exists_true():
    with patch("uplinkmgr.monitor.subprocess.run") as m:
        m.return_value.stdout = b"default via fe80::1 dev eth0\n"
        result = monitor.ipv6_default_route_exists(161, "eth0")
    assert result is True
    cmd = m.call_args[0][0]
    assert "161" in cmd
    assert "eth0" in cmd
    assert "default" in cmd


def test_ipv6_default_route_exists_false():
    with patch("uplinkmgr.monitor.subprocess.run") as m:
        m.return_value.stdout = b""
        result = monitor.ipv6_default_route_exists(161, "eth0")
    assert result is False


def test_ipv6_default_route_exists_whitespace_only():
    with patch("uplinkmgr.monitor.subprocess.run") as m:
        m.return_value.stdout = b"   \n"
        result = monitor.ipv6_default_route_exists(161, "eth0")
    assert result is False
