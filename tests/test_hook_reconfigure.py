"""Tests for hooks/50-uplinkmgr's RECONFIGURE handling.

Invokes the hook script directly as a subprocess (no root or real dhcpcd
required) to verify it replays state correctly -- and safely -- for a
`dhcpcd -g` (reconfigure) event.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_HOOK_PATH = str(Path(__file__).parent.parent / "hooks" / "50-uplinkmgr")


def _run_hook(env_dir: Path, state_dir: Path, interface: str, reason: str,
              **extra_env: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UPLINKMGR_ENV_DIR": str(env_dir),
        "UPLINKMGR_STATE_DIR": str(state_dir),
        "interface": interface,
        "reason": reason,
    }
    env.update({k: str(v) for k, v in extra_env.items()})
    return subprocess.run(["sh", _HOOK_PATH], env=env, capture_output=True, text=True)


@pytest.fixture
def env_dir(tmp_path):
    d = tmp_path / "envdir"
    d.mkdir()
    (d / "eth0.env").write_text(
        "UPLINKMGR_UPLINK_NAME=isp1\n"
        "UPLINKMGR_WAN_IFACE=eth0\n"
        "UPLINKMGR_IPV6_PD=true\n"
        "UPLINKMGR_IPV6_IA_NA=false\n"
    )
    return d


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _names(state_dir: Path) -> list[str]:
    return sorted(p.name for p in state_dir.iterdir())


class TestReconfigure:
    def test_ipv4_only_writes_only_ipv4_state(self, env_dir, state_dir):
        result = _run_hook(
            env_dir, state_dir, "eth0", "RECONFIGURE",
            new_routers="192.0.2.1 192.0.2.2",
            new_ip_address="192.0.2.100",
        )
        assert result.returncode == 0, result.stderr
        assert _names(state_dir) == ["isp1.ipv4.state"]
        content = (state_dir / "isp1.ipv4.state").read_text()
        assert "gateway=192.0.2.1" in content
        assert "address=192.0.2.100" in content

    def test_ra_only_writes_only_ra_state(self, env_dir, state_dir):
        result = _run_hook(
            env_dir, state_dir, "eth0", "RECONFIGURE",
            nd1_from="fe80::1",
            nd1_lifetime="1800",
            nd1_flags="",
            nd1_addr1="2001:db8::abcd",
            nd1_prefix_information1_prefix="2001:db8::",
            nd1_prefix_information1_length="64",
        )
        assert result.returncode == 0, result.stderr
        assert _names(state_dir) == ["isp1.ipv6ra.state"]

    def test_pd_and_na_writes_both_state_files(self, env_dir, state_dir):
        result = _run_hook(
            env_dir, state_dir, "eth0", "RECONFIGURE",
            new_dhcp6_ia_pd1_prefix1="2001:db8:aaaa::",
            new_dhcp6_ia_pd1_prefix1_length="56",
            new_dhcp6_ia_pd1_prefix1_vltime="86400",
            new_dhcp6_ia_pd1_prefix1_pltime="14400",
            new_dhcp6_ia_na1_ia_addr1="2001:db8::1",
        )
        assert result.returncode == 0, result.stderr
        assert _names(state_dir) == ["isp1.ipv6na.state", "isp1.ipv6pd.state"]

    def test_all_groups_together_writes_all_state_files(self, env_dir, state_dir):
        result = _run_hook(
            env_dir, state_dir, "eth0", "RECONFIGURE",
            new_routers="192.0.2.1",
            new_ip_address="192.0.2.100",
            nd1_from="fe80::1",
            nd1_lifetime="1800",
            nd1_flags="",
            new_dhcp6_ia_pd1_prefix1="2001:db8:aaaa::",
            new_dhcp6_ia_pd1_prefix1_length="56",
            new_dhcp6_ia_pd1_prefix1_vltime="86400",
            new_dhcp6_ia_pd1_prefix1_pltime="14400",
        )
        assert result.returncode == 0, result.stderr
        assert _names(state_dir) == [
            "isp1.ipv4.state", "isp1.ipv6pd.state", "isp1.ipv6ra.state",
        ]

    def test_nothing_present_writes_nothing(self, env_dir, state_dir):
        result = _run_hook(env_dir, state_dir, "eth0", "RECONFIGURE")
        assert result.returncode == 0, result.stderr
        assert _names(state_dir) == []

    def test_does_not_delete_existing_ipv6na_state_when_var_absent(self, env_dir, state_dir):
        # Pre-existing IA_NA state from a real prior BOUND6 event -- a
        # RECONFIGURE replay that doesn't populate the IA_NA variable must
        # not be treated as "address withdrawn".
        (state_dir / "isp1.ipv6na.state").write_text("address=2001:db8::9999\n")

        result = _run_hook(
            env_dir, state_dir, "eth0", "RECONFIGURE",
            new_dhcp6_ia_pd1_prefix1="2001:db8:aaaa::",
            new_dhcp6_ia_pd1_prefix1_length="56",
            new_dhcp6_ia_pd1_prefix1_vltime="86400",
            new_dhcp6_ia_pd1_prefix1_pltime="14400",
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir / "isp1.ipv6na.state").read_text() == "address=2001:db8::9999\n"

    def test_real_bound6_still_deletes_ipv6na_state_when_absent(self, env_dir, state_dir):
        # Regression check: the defensive skip is RECONFIGURE-only; a real
        # BOUND6 event must still delete stale IA_NA state as before.
        (state_dir / "isp1.ipv6na.state").write_text("address=2001:db8::9999\n")

        result = _run_hook(
            env_dir, state_dir, "eth0", "BOUND6",
            new_dhcp6_ia_pd1_prefix1="2001:db8:aaaa::",
            new_dhcp6_ia_pd1_prefix1_length="56",
            new_dhcp6_ia_pd1_prefix1_vltime="86400",
            new_dhcp6_ia_pd1_prefix1_pltime="14400",
        )

        assert result.returncode == 0, result.stderr
        assert not (state_dir / "isp1.ipv6na.state").exists()
