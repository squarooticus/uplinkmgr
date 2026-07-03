"""Tests for state.py — state file parsing and lifetime calculations."""

from unittest.mock import patch

import pytest
from tests.conftest import write_state
from uplinkmgr.state import (
    read_ipv4_state, read_ipv6ra_state, read_ipv6na_state, read_ipv6pd_state,
    write_atomic,
    IPv4State, IPv6RaState, IPv6NaState, IPv6PdState,
)


# --- write_atomic ---

class TestWriteAtomic:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.txt"
        write_atomic(str(target), "hello\n")
        assert target.read_text() == "hello\n"

    def test_no_leftover_tmp_file(self, tmp_path):
        target = tmp_path / "out.txt"
        write_atomic(str(target), "hello\n")
        assert not (tmp_path / "out.txt.tmp").exists()

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "out.txt"
        target.write_text("old\n")
        write_atomic(str(target), "new\n")
        assert target.read_text() == "new\n"

    def test_oserror_logged_not_raised(self, tmp_path):
        target = tmp_path / "missing_dir" / "out.txt"  # parent doesn't exist
        with patch("uplinkmgr.state.log") as log:
            write_atomic(str(target), "hello\n")  # must not raise
        assert log.error.called
        assert not target.exists()


# --- IPv4State ---

def test_read_ipv4_state_valid(tmp_path):
    write_state(tmp_path, "isp", "ipv4", {"gateway": "192.168.1.1", "address": "10.0.0.2"})
    st = read_ipv4_state(str(tmp_path), "isp")
    assert st == IPv4State(gateway="192.168.1.1", address="10.0.0.2")


def test_read_ipv4_state_missing_file(tmp_path):
    assert read_ipv4_state(str(tmp_path), "isp") is None


def test_read_ipv4_state_missing_required_key(tmp_path):
    write_state(tmp_path, "isp", "ipv4", {"gateway": "192.168.1.1"})  # no 'address'
    assert read_ipv4_state(str(tmp_path), "isp") is None


def test_read_ipv4_state_empty_file(tmp_path):
    (tmp_path / "isp.ipv4.state").write_text("")
    assert read_ipv4_state(str(tmp_path), "isp") is None


# --- IPv6RaState ---

def test_read_ipv6ra_state_full(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1",
        "lifetime": "3600",
        "timestamp": "1700000000",
        "address": "2001:db8::1",
        "prefix": "2001:db8::",
        "plen": "64",
    })
    st = read_ipv6ra_state(str(tmp_path), "isp")
    assert st is not None
    assert st.gateway == "fe80::1"
    assert st.lifetime == 3600
    assert st.timestamp == 1700000000
    assert st.address == "2001:db8::1"
    assert st.prefix == "2001:db8::"
    assert st.plen == 64


def test_read_ipv6ra_state_optional_fields_default(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1",
        "lifetime": "0",
        "timestamp": "1700000000",
        # address, prefix, plen omitted
    })
    st = read_ipv6ra_state(str(tmp_path), "isp")
    assert st is not None
    assert st.address == ""
    assert st.prefix == ""
    assert st.plen == 0


def test_read_ipv6ra_state_missing_required_key(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1",
        "lifetime": "3600",
        # 'timestamp' missing
    })
    assert read_ipv6ra_state(str(tmp_path), "isp") is None


def test_read_ipv6ra_state_missing_file(tmp_path):
    assert read_ipv6ra_state(str(tmp_path), "isp") is None


def test_read_ipv6ra_state_bad_int(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1",
        "lifetime": "notanumber",
        "timestamp": "1700000000",
    })
    assert read_ipv6ra_state(str(tmp_path), "isp") is None


# --- IPv6NaState ---

def test_read_ipv6na_state_valid(tmp_path):
    write_state(tmp_path, "isp", "ipv6na", {"address": "2001:db8::100"})
    st = read_ipv6na_state(str(tmp_path), "isp")
    assert st == IPv6NaState(address="2001:db8::100")


def test_read_ipv6na_state_missing_file(tmp_path):
    assert read_ipv6na_state(str(tmp_path), "isp") is None


# --- IPv6PdState ---

def test_read_ipv6pd_state_valid(tmp_path):
    write_state(tmp_path, "isp", "ipv6pd", {
        "delegated_prefix": "2001:db8:1::",
        "delegated_length": "56",
        "vltime": "7200",
        "pltime": "3600",
        "timestamp": "1700000000",
    })
    st = read_ipv6pd_state(str(tmp_path), "isp")
    assert st is not None
    assert st.delegated_prefix == "2001:db8:1::"
    assert st.delegated_length == 56
    assert st.vltime == 7200
    assert st.pltime == 3600
    assert st.timestamp == 1700000000


def test_read_ipv6pd_state_missing_file(tmp_path):
    assert read_ipv6pd_state(str(tmp_path), "isp") is None


# --- Lifetime calculations ---

def test_remaining_lifetime_infinite(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1", "lifetime": "0", "timestamp": "1700000000",
    })
    st = read_ipv6ra_state(str(tmp_path), "isp")
    assert st.remaining_lifetime(1700010000) == 0  # 0 = infinite sentinel


def test_remaining_lifetime_normal(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1", "lifetime": "3600", "timestamp": "1700000000",
    })
    st = read_ipv6ra_state(str(tmp_path), "isp")
    assert st.remaining_lifetime(1700000100) == 3500  # 100s elapsed


def test_remaining_lifetime_expired(tmp_path):
    write_state(tmp_path, "isp", "ipv6ra", {
        "gateway": "fe80::1", "lifetime": "3600", "timestamp": "1700000000",
    })
    st = read_ipv6ra_state(str(tmp_path), "isp")
    assert st.remaining_lifetime(1700004000) == 0  # past expiry → clamped to 0


def test_remaining_vltime_and_pltime(tmp_path):
    write_state(tmp_path, "isp", "ipv6pd", {
        "delegated_prefix": "2001:db8::",
        "delegated_length": "56",
        "vltime": "7200",
        "pltime": "3600",
        "timestamp": "1700000000",
    })
    st = read_ipv6pd_state(str(tmp_path), "isp")
    assert st.remaining_vltime(1700001000) == 6200
    assert st.remaining_pltime(1700001000) == 2600


def test_remaining_pltime_expired(tmp_path):
    write_state(tmp_path, "isp", "ipv6pd", {
        "delegated_prefix": "2001:db8::",
        "delegated_length": "56",
        "vltime": "7200",
        "pltime": "3600",
        "timestamp": "1700000000",
    })
    st = read_ipv6pd_state(str(tmp_path), "isp")
    assert st.remaining_pltime(1700005000) == 0  # expired
