"""Tests for daemon.py — reconcile logic and cleanup, with all syscalls mocked."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from tests.conftest import make_config, make_network, make_uplink, write_state
from uplinkmgr.daemon import Daemon
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

    def test_pd_state_installs_fwd_to_uplink_rules(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
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

        r.add_ipv6_fwd_to_uplink_rule.assert_called_once()
        assert "eth1-u0" in d._installed["isp"].macvlan_fwd

    def test_pd_state_absent_after_install_removes_macvlan_rules(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
        )
        d = _make_daemon(cfg, tmp_path)
        d._installed["isp"].macvlan_fwd["eth1-u0"] = None

        with patch("uplinkmgr.daemon.routing") as r:
            d._reconcile_uplink_ipv6(cfg.uplinks[0])

        r.del_ipv6_rule.assert_called()
        assert "eth1-u0" not in d._installed["isp"].macvlan_fwd

    def test_reject_incompatible_src_installs_prohibit_rules(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp", "eth0", index=0, ipv6_pd=True)],
            reject_incompatible_src=True,
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

        r.add_ipv6_prohibit_wrong_src_rule.assert_called_once()

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
