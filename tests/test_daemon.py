"""Tests for daemon.py — reconcile logic and cleanup, with all syscalls mocked."""

from __future__ import annotations

import logging
import subprocess
import time
from unittest.mock import patch
import pytest

from tests.conftest import make_config, make_network, make_uplink, write_state
from uplinkmgr import naming
from uplinkmgr.daemon import Daemon, log as daemon_log
from uplinkmgr.statemachine import LinkState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_daemon(cfg, tmp_path) -> Daemon:
    d = Daemon(config_path="unused", state_dir=str(tmp_path))
    d._cfg = cfg
    d._init_states()
    return d


# ---------------------------------------------------------------------------
# _reconfigure_dhcpcd
# ---------------------------------------------------------------------------

class TestReconfigureDhcpcd:
    def test_invokes_dhcpcd_g_when_dhcpcd_running(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        with patch.object(Daemon, "_dhcpcd_is_running", return_value=True):
            with patch("uplinkmgr.daemon.subprocess.run") as run:
                run.return_value.returncode = 0
                d._reconfigure_dhcpcd()

        run.assert_called_once_with(
            ["dhcpcd", "-g"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
        )

    def test_logs_warning_on_failure_without_raising(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        with patch.object(Daemon, "_dhcpcd_is_running", return_value=True):
            with patch("uplinkmgr.daemon.subprocess.run") as run:
                run.return_value.returncode = 1
                run.return_value.stderr = b"dhcpcd: not running\n"
                with patch("uplinkmgr.daemon.log") as log:
                    d._reconfigure_dhcpcd()  # must not raise

        assert log.warning.called

    def test_skips_when_dhcpcd_not_running(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        with patch.object(Daemon, "_dhcpcd_is_running", return_value=False):
            with patch("uplinkmgr.daemon.subprocess.run") as run:
                d._reconfigure_dhcpcd()

        run.assert_not_called()

    def test_timeout_logs_warning_without_raising(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        with patch.object(Daemon, "_dhcpcd_is_running", return_value=True):
            with patch("uplinkmgr.daemon.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="dhcpcd -g", timeout=10)):
                with patch("uplinkmgr.daemon.log") as log:
                    d._reconfigure_dhcpcd()  # must not raise

        assert log.warning.called


# ---------------------------------------------------------------------------
# _write_hook_debug_flag
# ---------------------------------------------------------------------------

class TestWriteHookDebugFlag:
    def _debug_env_path(self, tmp_path) -> str:
        return naming.hook_debug_env_path(str(tmp_path))

    def test_writes_debug_env_when_debug_enabled(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        level = daemon_log.level
        daemon_log.setLevel(logging.DEBUG)
        try:
            d._write_hook_debug_flag()
        finally:
            daemon_log.setLevel(level)

        content = (tmp_path / "debug.env").read_text()
        assert content == f"UPLINKMGR_HOOK_LOG={naming.hook_log_path(str(tmp_path))}\n"

    def test_removes_stale_debug_env_when_debug_disabled(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)
        (tmp_path / "debug.env").write_text("UPLINKMGR_HOOK_LOG=/run/uplinkmgr/hook.log\n")

        level = daemon_log.level
        daemon_log.setLevel(logging.INFO)
        try:
            d._write_hook_debug_flag()
        finally:
            daemon_log.setLevel(level)

        assert not (tmp_path / "debug.env").exists()

    def test_no_error_when_debug_disabled_and_no_file_present(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        level = daemon_log.level
        daemon_log.setLevel(logging.INFO)
        try:
            d._write_hook_debug_flag()  # must not raise
        finally:
            daemon_log.setLevel(level)

        assert not (tmp_path / "debug.env").exists()


class TestDhcpcdIsRunning:
    def test_true_when_pid_file_has_live_pid(self, tmp_path):
        with patch("uplinkmgr.daemon.Path") as MockPath:
            MockPath.return_value.read_text.return_value = "12345\n"
            with patch("uplinkmgr.daemon.os.kill") as kill:
                assert Daemon._dhcpcd_is_running() is True
        kill.assert_called_once_with(12345, 0)

    def test_false_when_pid_file_absent(self, tmp_path):
        with patch("uplinkmgr.daemon.Path") as MockPath:
            MockPath.return_value.read_text.side_effect = OSError("no such file")
            assert Daemon._dhcpcd_is_running() is False

    def test_false_when_pid_file_malformed(self, tmp_path):
        with patch("uplinkmgr.daemon.Path") as MockPath:
            MockPath.return_value.read_text.return_value = "not-a-pid\n"
            assert Daemon._dhcpcd_is_running() is False

    def test_false_when_pid_stale(self, tmp_path):
        with patch("uplinkmgr.daemon.Path") as MockPath:
            MockPath.return_value.read_text.return_value = "12345\n"
            with patch("uplinkmgr.daemon.os.kill", side_effect=ProcessLookupError()):
                assert Daemon._dhcpcd_is_running() is False

    def test_true_when_pid_exists_but_unsignallable(self, tmp_path):
        # os.kill(pid, 0) raises PermissionError when the process exists but
        # is owned by a different user (e.g. dhcpcd running as root while
        # this check runs as a less-privileged user) -- that confirms
        # existence, it doesn't deny it.
        with patch("uplinkmgr.daemon.Path") as MockPath:
            MockPath.return_value.read_text.return_value = "12345\n"
            with patch("uplinkmgr.daemon.os.kill", side_effect=PermissionError()):
                assert Daemon._dhcpcd_is_running() is True


# ---------------------------------------------------------------------------
# _do_reload
# ---------------------------------------------------------------------------

class TestDoReload:
    def test_reload_reinstalls_static_macvlan_fwd_to_uplink_rules(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
        )
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.load_config", return_value=cfg):
            with patch("uplinkmgr.daemon.routing") as r:
                d._do_reload()

        r.add_ipv6_fwd_to_uplink_rule.assert_called_once()
        assert d._installed["isp"].macvlan_fwd["eth1-u0"] is None

    def test_reload_resets_probe_timer(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)
        d._last_probe = time.monotonic()

        with patch("uplinkmgr.daemon.load_config", return_value=cfg):
            with patch("uplinkmgr.daemon.routing"):
                d._do_reload()

        assert d._last_probe == float("-inf")


# ---------------------------------------------------------------------------
# _loop probe timing
# ---------------------------------------------------------------------------

class TestLoop:
    def _run_one_iteration(self, d):
        """Run _loop for exactly one iteration: the _sleep stub stops the loop."""
        def stop(seconds):
            d._running = False
        with patch.object(d, "_sleep", side_effect=stop):
            with patch.object(d, "_run_cycle") as cycle:
                d._loop()
        return cycle

    def test_first_iteration_probes_immediately(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        cycle = self._run_one_iteration(d)

        cycle.assert_called_once()
        assert d._last_probe > float("-inf")

    def test_signal_wake_within_interval_does_not_probe(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)
        d._last_probe = time.monotonic()
        d._reconcile_requested = True

        with patch.object(d, "_do_reconcile") as reconcile:
            cycle = self._run_one_iteration(d)

        reconcile.assert_called_once()
        cycle.assert_not_called()

    def test_probe_runs_when_interval_elapsed(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)
        d._last_probe = time.monotonic() - cfg.monitor.interval - 1

        before = time.monotonic()
        cycle = self._run_one_iteration(d)

        cycle.assert_called_once()
        assert d._last_probe >= before


# ---------------------------------------------------------------------------
# Reconcile IPv4
# ---------------------------------------------------------------------------

class TestReconcileIPv4:
    def test_state_file_present_and_up_installs_routes_and_rule(self, tmp_path):
        cfg = make_config()
        write_state(tmp_path, "isp", "ipv4", {"gateway": "10.0.0.1", "address": "10.0.0.5"})
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv4(cfg.uplinks[0])

        r.replace_ipv4_route.assert_any_call("10.0.0.1", "eth0", 100, 160)   # shared table
        r.replace_ipv4_route.assert_any_call("10.0.0.1", "eth0", 0, 161)     # per-uplink table
        r.add_ipv4_lo_to_uplink_rule.assert_called_once()

    def test_state_file_absent_deletes_routes_and_rule(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv4_installed = "10.0.0.1"
        d._installed["isp"].ipv4_lo_to_uplink_addr = "10.0.0.5"

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv4(cfg.uplinks[0])

        r.del_ipv4_route.assert_any_call("eth0", 160)
        r.del_ipv4_route.assert_any_call("eth0", 161)
        r.del_ipv4_rule.assert_called_once()

    def test_state_file_present_but_down_removes_routes(self, tmp_path):
        cfg = make_config()
        write_state(tmp_path, "isp", "ipv4", {"gateway": "10.0.0.1", "address": "10.0.0.5"})
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv4_installed = "10.0.0.1"
        d._installed["isp"].ipv4_lo_to_uplink_addr = "10.0.0.5"
        d._states["isp"].ipv4 = LinkState.DOWN

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv4(cfg.uplinks[0])

        r.del_ipv4_route.assert_called()
        r.replace_ipv4_route.assert_not_called()

    def test_address_change_replaces_lo_to_uplink_rule(self, tmp_path):
        cfg = make_config()
        write_state(tmp_path, "isp", "ipv4", {"gateway": "10.0.0.1", "address": "10.0.0.99"})
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv4_installed = "10.0.0.1"
        d._installed["isp"].ipv4_lo_to_uplink_addr = "10.0.0.5"  # old address

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv4(cfg.uplinks[0])

        r.del_ipv4_rule.assert_called_once()
        r.add_ipv4_lo_to_uplink_rule.assert_called_once()
        assert r.add_ipv4_lo_to_uplink_rule.call_args[0][0] == "10.0.0.99"

    def test_no_change_when_already_installed(self, tmp_path):
        cfg = make_config()
        write_state(tmp_path, "isp", "ipv4", {"gateway": "10.0.0.1", "address": "10.0.0.5"})
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv4_installed = "10.0.0.1"
        d._installed["isp"].ipv4_lo_to_uplink_addr = "10.0.0.5"

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv4(cfg.uplinks[0])

        r.replace_ipv4_route.assert_not_called()
        r.del_ipv4_route.assert_not_called()
        r.add_ipv4_lo_to_uplink_rule.assert_not_called()
        r.del_ipv4_rule.assert_not_called()


# ---------------------------------------------------------------------------
# Reconcile IPv6
# ---------------------------------------------------------------------------

class TestReconcileIPv6:
    def test_ra_state_installs_ipv6_route(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        write_state(tmp_path, "isp", "ipv6ra", {
            "gateway": "fe80::1", "lifetime": "3600",
            "timestamp": "1000000", "address": "", "prefix": "2001:db8::", "plen": "56",
        })
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.replace_ipv6_route.assert_called_once()
        assert d._installed["isp"].ipv6_route_installed

    def test_ra_state_absent_after_install_removes_route(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv6_route_installed = True

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_route.assert_called_once()
        assert not d._installed["isp"].ipv6_route_installed

    def test_setup_installs_fwd_to_uplink_rule_for_every_macvlan(self, tmp_path):
        # fwd_to_uplink is static: installed once at startup by
        # _setup_ipv6_macvlan_rules, with no state files present at all.
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
        )
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._setup_ipv6_macvlan_rules()

        r.add_ipv6_fwd_to_uplink_rule.assert_called_once()
        mv, tbl, prio, prefix = r.add_ipv6_fwd_to_uplink_rule.call_args.args
        assert mv == "eth1-u0"
        assert prefix is None
        assert d._installed["isp"].macvlan_fwd["eth1-u0"] is None

    def test_pd_state_with_reject_wrong_pd_src_narrows_fwd_to_uplink_rule(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
            reject_wrong_pd_src=True,
        )
        write_state(tmp_path, "isp", "ipv6pd", {
            "delegated_prefix": "2001:db8::", "delegated_length": "56",
            "vltime": "86400", "pltime": "14400", "timestamp": "1000000",
        })
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].macvlan_fwd["eth1-u0"] = None  # as installed by setup

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_rule.assert_called_once()
        r.add_ipv6_fwd_to_uplink_rule.assert_called_once()
        assert d._installed["isp"].macvlan_fwd["eth1-u0"] == "2001:db8::/56"

    def test_pd_state_absent_after_install_persists_fwd_to_uplink_rule(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
        )
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].macvlan_fwd["eth1-u0"] = None  # as installed by setup

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_rule.assert_not_called()
        r.add_ipv6_fwd_to_uplink_rule.assert_not_called()
        assert "eth1-u0" in d._installed["isp"].macvlan_fwd

    def test_pd_state_absent_removes_prohibit_rule_but_not_fwd_to_uplink(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
            reject_wrong_pd_src=True,
        )
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].macvlan_fwd["eth1-u0"] = "2001:db8::/56"
        d._installed["isp"].macvlan_prohibit.add("eth1-u0")

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_rule.assert_called_once()
        assert "eth1-u0" in d._installed["isp"].macvlan_fwd
        assert "eth1-u0" not in d._installed["isp"].macvlan_prohibit

    def test_pd_prefix_rotation_replaces_fwd_to_uplink_in_place(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
            reject_wrong_pd_src=True,
        )
        write_state(tmp_path, "isp", "ipv6pd", {
            "delegated_prefix": "2001:db8:bbbb::", "delegated_length": "56",
            "vltime": "86400", "pltime": "14400", "timestamp": "1000000",
        })
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].macvlan_fwd["eth1-u0"] = "2001:db8:aaaa::/56"

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_rule.assert_called_once()
        r.add_ipv6_fwd_to_uplink_rule.assert_called_once()
        assert d._installed["isp"].macvlan_fwd["eth1-u0"] == "2001:db8:bbbb::/56"

    def test_reject_wrong_pd_src_installs_prohibit_rules(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
            reject_wrong_pd_src=True,
        )
        write_state(tmp_path, "isp", "ipv6ra", {
            "gateway": "fe80::1", "lifetime": "0",
            "timestamp": "0", "address": "", "prefix": "", "plen": "0",
        })
        write_state(tmp_path, "isp", "ipv6pd", {
            "delegated_prefix": "2001:db8::", "delegated_length": "56",
            "vltime": "86400", "pltime": "14400", "timestamp": "1000000",
        })
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.add_ipv6_reject_wrong_pd_src_rule.assert_called_once()

    def test_ia_na_only_uplink_installs_route(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ia_na=True, ipv6_pd=False)])
        write_state(tmp_path, "isp", "ipv6ra", {
            "gateway": "fe80::1", "lifetime": "3600",
            "timestamp": "1000000", "address": "", "prefix": "2001:db8::", "plen": "64",
        })
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.replace_ipv6_route.assert_called_once()


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

class TestTeardownAll:
    def test_installed_routes_and_rules_removed(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv4_installed = "10.0.0.1"
        d._installed["isp"].ipv4_lo_to_uplink_addr = "10.0.0.5"
        d._installed["isp"].ipv6_route_installed = True
        d._ipv4_rules_installed = True
        d._ipv6_rule_installed = True

        with patch("uplinkmgr.daemon.routing") as r:
            d._teardown_all()

        r.del_ipv4_route.assert_called()
        r.del_ipv4_rule.assert_called()
        r.del_ipv6_route.assert_called()
        r.del_ipv4_policy_rules.assert_called_once()
        r.del_ipv6_policy_rule.assert_called_once()

    def test_ia_na_only_ipv6_route_is_torn_down(self, tmp_path):
        """Regression for Bug B: _teardown_all must handle ia_na-only uplinks."""
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ia_na=True, ipv6_pd=False)])
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].ipv6_route_installed = True
        d._ipv4_rules_installed = True
        d._ipv6_rule_installed = True

        with patch("uplinkmgr.daemon.routing") as r:
            d._teardown_all()

        r.del_ipv6_route.assert_called_once()

    def test_nothing_called_when_nothing_installed(self, tmp_path):
        cfg = make_config()
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.routing") as r:
            d._teardown_all()

        r.del_ipv4_route.assert_not_called()
        r.del_ipv4_rule.assert_not_called()
        r.del_ipv6_route.assert_not_called()
        r.del_ipv4_policy_rules.assert_not_called()
        r.del_ipv6_policy_rule.assert_not_called()


# ---------------------------------------------------------------------------
# Cleanup (radvd reset on stop)
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_calls_radvd_regenerate_all_with_all_up(self, tmp_path):
        """Regression for Bug A: _cleanup must regenerate radvd configs in all-UP state."""
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        d = _make_daemon(cfg, tmp_path)
        d._ipv4_rules_installed = False
        d._ipv6_rule_installed = False

        with patch("uplinkmgr.daemon.routing"), \
             patch("uplinkmgr.daemon.radvd") as mock_radvd:
            d._cleanup()

        mock_radvd.regenerate_all.assert_called_once()
        call_kwargs = mock_radvd.regenerate_all.call_args[1]
        assert call_kwargs["action"] == "sighup"
        states = call_kwargs["states"]
        assert states["isp"].ipv6 == LinkState.UP

    def test_cleanup_radvd_uses_all_up_even_when_uplink_was_down(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        d = _make_daemon(cfg, tmp_path)
        d._states["isp"].ipv6 = LinkState.DOWN

        with patch("uplinkmgr.daemon.routing"), \
             patch("uplinkmgr.daemon.radvd") as mock_radvd:
            d._cleanup()

        states = mock_radvd.regenerate_all.call_args[1]["states"]
        assert states["isp"].ipv6 == LinkState.UP


# ---------------------------------------------------------------------------
# _probe_uplink
# ---------------------------------------------------------------------------

class TestProbeUplink:
    def test_probes_ipv6_even_with_no_route_state(self, tmp_path):
        """No ipv6ra.state file at all -- previously this would have skipped
        the IPv6 probe entirely; now it must still run and let the kernel's
        own routing (including fallback to the main table) decide reachability."""
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)])
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = True
            m.probe_ipv6.return_value = False
            name, ipv4_ok, ipv6_ok, ipv6_probe_enabled = d._probe_uplink(cfg.uplinks[0])

        m.probe_ipv6.assert_called_once()
        assert ipv6_probe_enabled is True
        assert ipv6_ok is False
        assert name == "isp"
        assert ipv4_ok is True

    def test_skips_ipv6_probe_when_uplink_has_no_ipv6(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0,
                                                 ipv6_pd=False, ia_na=False)])
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = True
            name, ipv4_ok, ipv6_ok, ipv6_probe_enabled = d._probe_uplink(cfg.uplinks[0])

        m.probe_ipv6.assert_not_called()
        assert ipv6_probe_enabled is False
        assert ipv6_ok is True  # default, unused by sm_update when disabled

    def test_probes_ipv6_for_ia_na_only_uplink(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0,
                                                 ipv6_pd=False, ia_na=True)])
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = True
            m.probe_ipv6.return_value = True
            _, _, ipv6_ok, ipv6_probe_enabled = d._probe_uplink(cfg.uplinks[0])

        m.probe_ipv6.assert_called_once()
        assert ipv6_probe_enabled is True
        assert ipv6_ok is True

    def test_ipv4_always_probed_regardless_of_ipv6_config(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0,
                                                 ipv6_pd=False, ia_na=False)])
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = False
            d._probe_uplink(cfg.uplinks[0])

        m.probe_ipv4.assert_called_once()

    def test_probe_ipv6_passes_known_ia_na_address(self, tmp_path):
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0,
                                                 ipv6_pd=False, ia_na=True)])
        d = _make_daemon(cfg, tmp_path)
        write_state(tmp_path, "isp", "ipv6na", {"address": "2602:107:6511:3d:d69:2d19:b22:5322"})

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = True
            m.probe_ipv6.return_value = True
            d._probe_uplink(cfg.uplinks[0])

        assert m.probe_ipv6.call_args[0][3] == "2602:107:6511:3d:d69:2d19:b22:5322"

    def test_probe_ipv6_passes_none_without_ia_na_state(self, tmp_path):
        """SLAAC-only uplinks (no IA_NA) have no single fixed address to bind
        to, so the probe falls back to -I <iface> alone."""
        cfg = make_config(uplinks=[make_uplink("isp", "eth0", index=0,
                                                 ipv6_pd=True, ia_na=False)])
        d = _make_daemon(cfg, tmp_path)

        with patch("uplinkmgr.daemon.monitor") as m:
            m.probe_ipv4.return_value = True
            m.probe_ipv6.return_value = True
            d._probe_uplink(cfg.uplinks[0])

        assert m.probe_ipv6.call_args[0][3] is None
