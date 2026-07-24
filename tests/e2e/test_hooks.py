"""E2E tests: event hooks fire on real wan-down/wan-up/reload transitions.

Run with: sudo pytest -m e2e tests/e2e/test_hooks.py -v
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
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
_FAILOVER_WAIT = 12   # interval=3, threshold=2 -> 6s min + buffer
_DAEMON_BIN = str(Path(__file__).parent.parent.parent / "bin" / "uplinkmgr")
_SRC_DIR = str(Path(__file__).parent.parent.parent / "src")


def _start_dhcp4_in_ns(topo, ns_name, iface, server_ip, client_ip, gateway):
    log_dir = Path(topo.state_dir).parent
    log = open(str(log_dir / f"dhcp4-{ns_name}.log"), "w")
    cmd = ["ip", "netns", "exec", ns_name,
           _PYTHON, str(_HELPERS_DIR / "dhcp4.py"),
           iface, server_ip, client_ip, gateway]
    return subprocess.Popen(cmd, stdout=log, stderr=log)


def _start_dhcpcd(topo, dhcpcd_conf):
    log_path = str(Path(dhcpcd_conf).parent / "dhcpcd.log")
    log = open(log_path, "w")
    cmd = ["ip", "netns", "exec", topo.ns_router,
           "dhcpcd", "--config", str(dhcpcd_conf), "--nobackground",
           "-d", "-j", log_path]
    return subprocess.Popen(cmd, stdout=log, stderr=log)


def _start_daemon(topo, cfg_path, hooks_system_dir, hooks_user_dir):
    log = open(str(Path(cfg_path).parent / "daemon.log"), "w")
    _pp = os.environ.get("PYTHONPATH", "")
    env = {**os.environ, "PYTHONPATH": f"{_SRC_DIR}:{_pp}" if _pp else _SRC_DIR}
    cmd = ["ip", "netns", "exec", topo.ns_router,
           _PYTHON, _DAEMON_BIN,
           "--config", str(cfg_path),
           "--state-dir", topo.state_dir,
           "--hooks-system-dir", str(hooks_system_dir),
           "--hooks-user-dir", str(hooks_user_dir),
           "--log-level", "DEBUG"]
    return subprocess.Popen(cmd, stdout=log, stderr=log, env=env)


def _wait_state(topo, name, kind, timeout=15):
    return ns.wait_for_file(f"{topo.state_dir}/{name}.{kind}.state", timeout=timeout)


def _stop(procs):
    for p in reversed(procs):
        try:
            p.terminate(); p.wait(timeout=3)
        except (subprocess.TimeoutExpired, OSError):
            try: p.kill()
            except OSError: pass


def _write_marker_hook(hooks_dir: Path, marker_log: Path) -> Path:
    """A single event hook that appends one line per invocation to marker_log,
    covering everything a test might want to assert on."""
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = hooks_dir / "10-marker"
    script.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        printf 'event=%s argv1=%s argv2=%s uplink=%s family=%s config_path=%s\\n' \\
            "$UPLINKMGR_EVENT" "$1" "$2" "$UPLINKMGR_UPLINK" "$UPLINKMGR_FAMILY" \\
            "$UPLINKMGR_CONFIG_PATH" >> "{marker_log}"
    """))
    script.chmod(0o755)
    return script


def _wait_marker_line(marker_log: Path, needle: str, timeout: float = _FAILOVER_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker_log.exists() and needle in marker_log.read_text():
            return True
        time.sleep(0.25)
    return False


class TestEventHooks:
    def test_daemon_start_fires_on_launch(self, topology, tmp_path):
        """daemon-start fires once the daemon has completed its initial reconcile."""
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

            user_dir = tmp_path / "hooks" / "user"
            system_dir = tmp_path / "hooks" / "system"  # left empty/nonexistent
            marker = tmp_path / "marker.log"
            _write_marker_hook(user_dir, marker)

            procs.append(_start_daemon(topo, cfg, system_dir, user_dir))

            assert _wait_marker_line(marker, "event=daemon-start"), \
                f"daemon-start never fired: {marker.read_text() if marker.exists() else '(no marker file)'}"
        finally:
            _stop(procs)

    def test_wan_down_and_wan_up_fire_on_real_failover(self, topology, tmp_path):
        """Killing isp1's lease triggers wan-down; restoring it triggers wan-up."""
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

            user_dir = tmp_path / "hooks" / "user"
            system_dir = tmp_path / "hooks" / "system"
            marker = tmp_path / "marker.log"
            _write_marker_hook(user_dir, marker)

            procs.append(_start_daemon(topo, cfg, system_dir, user_dir))
            assert ns.wait_for_route(topo.ns_router, "160"), "initial routes not installed"

            # Simulate ISP1 failure
            isp1_dhcp.terminate(); isp1_dhcp.wait(timeout=3)
            procs.remove(isp1_dhcp)
            ns.ip_in(topo.ns_isp1, "link", "set", topo.wan1_isp, "down", check=False)

            assert _wait_marker_line(marker, "event=wan-down argv1=wan-down argv2=isp1 uplink=isp1 family=ipv4"), \
                f"wan-down never fired for isp1: {marker.read_text() if marker.exists() else '(no marker file)'}"

            # Bring isp1 back
            ns.ip_in(topo.ns_isp1, "link", "set", topo.wan1_isp, "up", check=False)
            isp1_dhcp = _start_dhcp4_in_ns(topo, topo.ns_isp1, topo.wan1_isp,
                                            topo.isp1_gw, topo.isp1_wan_ip, topo.isp1_gw)
            procs.append(isp1_dhcp)

            assert _wait_marker_line(marker, "event=wan-up argv1=wan-up argv2=isp1 uplink=isp1 family=ipv4"), \
                f"wan-up never fired for isp1 recovery: {marker.read_text()}"
        finally:
            _stop(procs)

    def test_reload_fires_on_sighup(self, topology, tmp_path):
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

            user_dir = tmp_path / "hooks" / "user"
            system_dir = tmp_path / "hooks" / "system"
            marker = tmp_path / "marker.log"
            _write_marker_hook(user_dir, marker)

            daemon = _start_daemon(topo, cfg, system_dir, user_dir)
            procs.append(daemon)
            assert ns.wait_for_route(topo.ns_router, "160"), "initial routes not installed"
            assert _wait_marker_line(marker, "event=daemon-start")

            daemon.send_signal(signal.SIGHUP)

            assert _wait_marker_line(marker, f"event=reload argv1=reload argv2= uplink= family= config_path={cfg}"), \
                f"reload never fired on SIGHUP: {marker.read_text()}"
        finally:
            _stop(procs)

    def test_daemon_stop_fires_before_pid_file_removed(self, topology, tmp_path):
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

            user_dir = tmp_path / "hooks" / "user"
            system_dir = tmp_path / "hooks" / "system"
            marker = tmp_path / "marker.log"
            _write_marker_hook(user_dir, marker)

            daemon = _start_daemon(topo, cfg, system_dir, user_dir)
            procs.append(daemon)
            assert ns.wait_for_route(topo.ns_router, "160"), "initial routes not installed"
            assert _wait_marker_line(marker, "event=daemon-start")

            daemon.terminate(); daemon.wait(timeout=5)
            procs.remove(daemon)

            assert "event=daemon-stop" in marker.read_text(), \
                f"daemon-stop never fired: {marker.read_text()}"
        finally:
            _stop(procs)
