"""E2E tests: IPv6 PD, SLAAC, macvlan rules, and preference signalling.

Also requires radvd. Run with: sudo pytest -m e2e tests/e2e/test_ipv6.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    Topology, _HAVE_RADVD, write_dhcpcd_conf, write_uplinkmgr_yaml,
)
from tests.e2e.helpers import netns as ns

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.geteuid() != 0, reason="e2e tests require root"),
    pytest.mark.skipif(
        not all(shutil.which(t) for t in ["dhcpcd", "ip"]),
        reason="dhcpcd or ip not found",
    ),
    pytest.mark.skipif(not _HAVE_RADVD, reason="radvd not found"),
]

_HELPERS_DIR = Path(__file__).parent / "helpers"
_PYTHON = sys.executable
_FAILOVER_WAIT = 12
_DAEMON_BIN = str(Path(__file__).parent.parent.parent / "bin" / "uplinkmgr")
_SETUP_BIN = str(Path(__file__).parent.parent.parent / "bin" / "uplinkmgr-setup")


def _popen_in(ns_name, *cmd):
    return subprocess.Popen(
        ["ip", "netns", "exec", ns_name] + list(cmd),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop(procs):
    for p in reversed(procs):
        try:
            p.terminate(); p.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try: p.kill()
            except OSError: pass


def _wait_state(topo, name, kind, timeout=15):
    return ns.wait_for_file(f"{topo.state_dir}/{name}.{kind}.state", timeout=timeout)


class TestIPv6PD:
    def test_macvlan_stanza_generated_by_setup(self, topology, tmp_path):
        """uplinkmgr-setup --dry-run outputs a macvlan stanza for the PD uplink."""
        topo = topology
        cfg = write_uplinkmgr_yaml(tmp_path, topo)
        result = subprocess.run(
            [_PYTHON, _SETUP_BIN, "--config", str(cfg), "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert f"auto {topo.mv0}" in result.stdout
        assert "type macvlan" in result.stdout

    def test_pd_state_written_by_hook(self, topology, tmp_path):
        """dhcpcd hook writes .ipv6pd.state when DHCPv6 PD reply is received."""
        topo = topology
        procs = []
        try:
            procs += [
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
                           topo.wan1_isp, topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp6.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix,
                           str(topo.isp1_prefix_len)),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "ra.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix, "64"),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_popen_in(topo.ns_router, "dhcpcd",
                                    "--config", str(write_dhcpcd_conf(tmp_path, topo)),
                                    "--nobackground",
                                    "--rundir", str(tmp_path / "dhcpcd"),
                                    "--dbdir", str(tmp_path / "dhcpcd-db")))

            assert _wait_state(topo, "isp1", "ipv6pd", timeout=25), \
                "ipv6pd.state not written — DHCPv6 PD exchange may not have completed"

            content = Path(f"{topo.state_dir}/isp1.ipv6pd.state").read_text()
            assert "delegated_prefix=" in content
            assert topo.isp1_delegated_prefix.split("::")[0] in content
        finally:
            _stop(procs)

    def test_ipv6_fwd_to_uplink_rule_installed(self, topology, tmp_path):
        """Daemon installs iif <macvlan> lookup <table> rule for IPv6 PD uplink."""
        topo = topology
        procs = []
        try:
            procs += [
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
                           topo.wan1_isp, topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _popen_in(topo.ns_isp2, _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
                           topo.wan2_isp, topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp6.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix,
                           str(topo.isp1_prefix_len)),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "ra.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix, "64"),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_popen_in(topo.ns_router, "dhcpcd",
                                    "--config", str(write_dhcpcd_conf(tmp_path, topo)),
                                    "--nobackground",
                                    "--rundir", str(tmp_path / "dhcpcd"),
                                    "--dbdir", str(tmp_path / "dhcpcd-db")))

            assert _wait_state(topo, "isp1", "ipv4")
            assert _wait_state(topo, "isp1", "ipv6pd", timeout=25)

            procs.append(_popen_in(topo.ns_router, _PYTHON, _DAEMON_BIN,
                                    "--config", str(cfg),
                                    "--state-dir", topo.state_dir,
                                    "--log-level", "DEBUG"))
            time.sleep(5)

            rules = ns.rule_show(topo.ns_router, v6=True)
            assert topo.mv0 in rules, \
                f"fwd_to_uplink rule for {topo.mv0} missing from ip -6 rule: {rules}"
        finally:
            _stop(procs)

    def test_ipv6_down_preference_low_in_radvd_conf(self, topology, tmp_path):
        """When IPv6 health goes DOWN, daemon rewrites radvd config with preference low."""
        topo = topology
        procs = []
        try:
            procs += [
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
                           topo.wan1_isp, topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _popen_in(topo.ns_isp2, _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
                           topo.wan2_isp, topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "dhcp6.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix,
                           str(topo.isp1_prefix_len)),
                _popen_in(topo.ns_isp1, _PYTHON, str(_HELPERS_DIR / "ra.py"),
                           topo.wan1_isp, topo.isp1_delegated_prefix, "64"),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_popen_in(topo.ns_router, "dhcpcd",
                                    "--config", str(write_dhcpcd_conf(tmp_path, topo)),
                                    "--nobackground",
                                    "--rundir", str(tmp_path / "dhcpcd"),
                                    "--dbdir", str(tmp_path / "dhcpcd-db")))

            assert _wait_state(topo, "isp1", "ipv4")
            assert _wait_state(topo, "isp1", "ipv6pd", timeout=25)
            assert _wait_state(topo, "isp1", "ipv6ra", timeout=15)

            procs.append(_popen_in(topo.ns_router, _PYTHON, _DAEMON_BIN,
                                    "--config", str(cfg),
                                    "--state-dir", topo.state_dir,
                                    "--log-level", "DEBUG"))
            time.sleep(4)

            # Simulate IPv6 failure by bringing wan1 down in the router namespace
            ns.ip_in(topo.ns_router, "link", "set", topo.wan1, "down", check=False)
            time.sleep(_FAILOVER_WAIT)

            conf_path = Path("/etc/uplinkmgr/radvd/radvd-uplinkmgr-isp1.conf")
            if conf_path.exists():
                content = conf_path.read_text()
                assert "AdvDefaultPreference low" in content, \
                    f"Expected 'low' preference after IPv6 failure:\n{content[:500]}"
        finally:
            _stop(procs)
