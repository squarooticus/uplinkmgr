"""E2E tests: IPv4 routing, failover, recovery, and cleanup.

Run with: sudo pytest -m e2e tests/e2e/test_failover.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import Topology, write_dhcpcd_conf, write_uplinkmgr_yaml
from tests.e2e.helpers import netns as ns

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.geteuid() != 0, reason="e2e tests require root"),
    pytest.mark.skipif(
        not all(shutil.which(t) for t in ["dhcpcd", "ip"]),
        reason="dhcpcd or ip not found",
    ),
]

_HELPERS_DIR = Path(__file__).parent / "helpers"
_PYTHON = sys.executable
_FAILOVER_WAIT = 12   # interval=3, threshold=2 → 6s min + buffer
_DAEMON_BIN = str(Path(__file__).parent.parent.parent / "bin" / "uplinkmgr")


def _start_dhcp4_in_ns(topo, ns_name, iface, server_ip, client_ip, gateway):
    cmd = ["ip", "netns", "exec", ns_name,
           _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
           iface, server_ip, client_ip, gateway]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _start_dhcpcd(topo, dhcpcd_conf):
    cmd = ["ip", "netns", "exec", topo.ns_router,
           "dhcpcd", "--config", str(dhcpcd_conf), "--nobackground",
           "--rundir", str(Path(dhcpcd_conf).parent / "dhcpcd"),
           "--dbdir", str(Path(dhcpcd_conf).parent / "dhcpcd-db")]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _start_daemon(topo, cfg_path):
    cmd = ["ip", "netns", "exec", topo.ns_router,
           _PYTHON, _DAEMON_BIN,
           "--config", str(cfg_path),
           "--state-dir", topo.state_dir,
           "--log-level", "DEBUG"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _wait_state(topo, name, kind, timeout=15):
    return ns.wait_for_file(f"{topo.state_dir}/{name}.{kind}.state", timeout=timeout)


def _stop(procs):
    for p in reversed(procs):
        try:
            p.terminate(); p.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try: p.kill()
            except OSError: pass


class TestIPv4Routing:
    def test_primary_uplink_route_installed(self, topology, tmp_path):
        """After DHCP leases obtained, isp1 route (metric 100) appears in table 160."""
        topo = topology
        procs = []
        try:
            procs += [
                _start_dhcp4_in_ns(topo, topo.ns_isp1, topo.wan1_isp,
                                    topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _start_dhcp4_in_ns(topo, topo.ns_isp2, topo.wan2_isp,
                                    topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            dhcpcd_conf = write_dhcpcd_conf(tmp_path, topo)
            procs.append(_start_dhcpcd(topo, dhcpcd_conf))

            assert _wait_state(topo, "isp1", "ipv4"), \
                "isp1 IPv4 state file not written"
            assert _wait_state(topo, "isp2", "ipv4"), \
                "isp2 IPv4 state file not written"

            procs.append(_start_daemon(topo, cfg))
            assert ns.wait_for_route(topo.ns_router, "160"), \
                "No default route in uplinkmgr table 160"

            routes = ns.route_show(topo.ns_router, "160")
            assert "metric 100" in routes, \
                f"isp1 route (metric 100) missing: {routes}"
        finally:
            _stop(procs)

    def test_lo_to_uplink_rules_installed(self, topology, tmp_path):
        """Daemon installs per-uplink from-<wan-ip> rules at priorities 29001/29002."""
        topo = topology
        procs = []
        try:
            procs += [
                _start_dhcp4_in_ns(topo, topo.ns_isp1, topo.wan1_isp,
                                    topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _start_dhcp4_in_ns(topo, topo.ns_isp2, topo.wan2_isp,
                                    topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_start_dhcpcd(topo, write_dhcpcd_conf(tmp_path, topo)))

            assert _wait_state(topo, "isp1", "ipv4")
            assert _wait_state(topo, "isp2", "ipv4")

            procs.append(_start_daemon(topo, cfg))
            time.sleep(3)

            rules = ns.rule_show(topo.ns_router)
            assert "29000" in rules, f"suppress rule missing: {rules}"
            assert "29001" in rules, f"isp1 lo_to_uplink rule missing: {rules}"
            assert "29002" in rules, f"isp2 lo_to_uplink rule missing: {rules}"
        finally:
            _stop(procs)

    def test_failover_removes_dead_uplink_route(self, topology, tmp_path):
        """When ISP1 goes down, its default route is removed from table 160."""
        topo = topology
        procs = []
        isp1_dhcp = None
        try:
            isp1_dhcp = _start_dhcp4_in_ns(topo, topo.ns_isp1, topo.wan1_isp,
                                             topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw)
            procs += [
                isp1_dhcp,
                _start_dhcp4_in_ns(topo, topo.ns_isp2, topo.wan2_isp,
                                    topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_start_dhcpcd(topo, write_dhcpcd_conf(tmp_path, topo)))

            assert _wait_state(topo, "isp1", "ipv4")
            assert _wait_state(topo, "isp2", "ipv4")

            procs.append(_start_daemon(topo, cfg))
            assert ns.wait_for_route(topo.ns_router, "160"), "initial routes not installed"

            # Simulate ISP1 failure
            isp1_dhcp.terminate(); isp1_dhcp.wait(timeout=3)
            procs.remove(isp1_dhcp)
            ns.ip_in(topo.ns_isp1, "link", "set", topo.wan1_isp, "down", check=False)

            assert ns.wait_for_route_gone(topo.ns_router, "160", topo.wan1,
                                           timeout=_FAILOVER_WAIT), \
                "isp1 route still in table 160 after failure"
            assert topo.wan2 in ns.route_show(topo.ns_router, "160"), \
                "isp2 fallback route missing"
        finally:
            _stop(procs)

    def test_cleanup_removes_rules_on_stop(self, topology, tmp_path):
        """After daemon stops, all uplinkmgr policy rules are removed."""
        topo = topology
        procs = []
        daemon = None
        try:
            procs += [
                _start_dhcp4_in_ns(topo, topo.ns_isp1, topo.wan1_isp,
                                    topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw),
                _start_dhcp4_in_ns(topo, topo.ns_isp2, topo.wan2_isp,
                                    topo.isp2_gw, topo.isp2_wan_ip, topo.isp2_gw),
            ]
            cfg = write_uplinkmgr_yaml(tmp_path, topo)
            procs.append(_start_dhcpcd(topo, write_dhcpcd_conf(tmp_path, topo)))

            assert _wait_state(topo, "isp1", "ipv4")
            assert _wait_state(topo, "isp2", "ipv4")

            daemon = _start_daemon(topo, cfg)
            procs.append(daemon)
            time.sleep(3)

            daemon.terminate(); daemon.wait(timeout=5)
            procs.remove(daemon)
            time.sleep(1)

            rules = ns.rule_show(topo.ns_router)
            assert "29000" not in rules, \
                f"suppress rule still present after daemon stop: {rules}"
            assert "29003" not in rules, \
                f"fwd_to_wan rule still present after daemon stop: {rules}"
        finally:
            _stop(procs)
