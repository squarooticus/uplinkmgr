"""Tests for radvd.py — regenerate_all preferred_lifetime logic."""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import make_config, make_uplink, make_network
from uplinkmgr.statemachine import LinkState, UplinkState
from uplinkmgr import radvd
from uplinkmgr.state import IPv6PdState, IPv6RaState


def _up_state(name: str) -> UplinkState:
    return UplinkState(name=name, ipv4=LinkState.UP, ipv6=LinkState.UP)


def _down_state(name: str) -> UplinkState:
    return UplinkState(name=name, ipv4=LinkState.DOWN, ipv6=LinkState.DOWN)


def _pd_state(pltime: int = 14400, vltime: int = 86400) -> IPv6PdState:
    return IPv6PdState(
        delegated_prefix="2001:db8::",
        delegated_length=56,
        vltime=vltime,
        pltime=pltime,
        timestamp=int(time.time()),
    )


def _ra_state(lifetime: int = 3600) -> IPv6RaState:
    return IPv6RaState(
        gateway="fe80::1",
        lifetime=lifetime,
        timestamp=int(time.time()),
        address="",
        prefix="",
        plen=0,
    )


def _run_regenerate_all(cfg, states, tmp_path, pd_states=None, ra_states=None):
    """Run regenerate_all with mocked state reads and file/signal operations.

    Returns {uplink_name: kwargs_dict} from radvd_conf_from_state calls.
    preferred_lifetime in kwargs is the value radvd.py computed; is_down=True means
    the generator will additionally zero it internally for DOWN uplinks.
    """
    uplink_names = [u.name for u in cfg.uplinks if u.ipv6_pd]
    if pd_states is None:
        pd_states = {name: _pd_state() for name in uplink_names}
    if ra_states is None:
        ra_states = {name: _ra_state() for name in uplink_names}

    calls = []

    def capture_conf(**kwargs):
        calls.append(kwargs)
        return ""

    with patch("uplinkmgr.radvd.read_ipv6pd_state",
               side_effect=lambda sd, name: pd_states.get(name)):
        with patch("uplinkmgr.radvd.read_ipv6ra_state",
                   side_effect=lambda sd, name: ra_states.get(name)):
            with patch("uplinkmgr.radvd.generator.radvd_conf_from_state",
                       side_effect=capture_conf):
                with patch("uplinkmgr.radvd._write_atomic"):
                    with patch("uplinkmgr.radvd._systemctl"):
                        with patch("uplinkmgr.radvd._sighup"):
                            radvd.regenerate_all(
                                cfg=cfg,
                                states=states,
                                state_dir=str(tmp_path),
                                action="write",
                            )

    return {c["uplink"].name: c for c in calls}


# ---------------------------------------------------------------------------
# exclusive_preferred_pd: False (default)
# ---------------------------------------------------------------------------

class TestExclusivePreferredPdDisabled:
    def test_both_up_both_get_positive_preferred_lifetime(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[
                make_uplink("isp1", "eth0", index=0, ipv6_pd=True),
                make_uplink("isp2", "eth3", index=1, ipv6_pd=True),
            ],
            exclusive_preferred_pd=False,
        )
        states = {"isp1": _up_state("isp1"), "isp2": _up_state("isp2")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0
        assert result["isp2"]["preferred_lifetime"] > 0

    def test_single_up_gets_positive_preferred_lifetime(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp1", "eth0", index=0, ipv6_pd=True)],
            exclusive_preferred_pd=False,
        )
        states = {"isp1": _up_state("isp1")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0


# ---------------------------------------------------------------------------
# exclusive_preferred_pd: True
# ---------------------------------------------------------------------------

class TestExclusivePreferredPdEnabled:
    def test_primary_up_gets_positive_secondary_up_gets_zero(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[
                make_uplink("isp1", "eth0", index=0, ipv6_pd=True),
                make_uplink("isp2", "eth3", index=1, ipv6_pd=True),
            ],
            exclusive_preferred_pd=True,
        )
        states = {"isp1": _up_state("isp1"), "isp2": _up_state("isp2")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0
        assert result["isp2"]["preferred_lifetime"] == 0

    def test_single_up_uplink_is_primary_gets_positive(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[make_uplink("isp1", "eth0", index=0, ipv6_pd=True)],
            exclusive_preferred_pd=True,
        )
        states = {"isp1": _up_state("isp1")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0

    def test_down_uplink_handled_via_is_down_not_secondary_path(self, tmp_path):
        # A DOWN uplink gets is_down=True passed to the generator (which zeros preferred/valid).
        # The exclusive_preferred_pd secondary path must not trigger for DOWN uplinks.
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[
                make_uplink("isp1", "eth0", index=0, ipv6_pd=True),
                make_uplink("isp2", "eth3", index=1, ipv6_pd=True),
            ],
            exclusive_preferred_pd=True,
        )
        states = {"isp1": _up_state("isp1"), "isp2": _down_state("isp2")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0
        assert result["isp2"]["is_down"] is True  # DOWN path, not secondary path

    def test_primary_down_secondary_becomes_new_primary_gets_positive(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[
                make_uplink("isp1", "eth0", index=0, ipv6_pd=True),
                make_uplink("isp2", "eth3", index=1, ipv6_pd=True),
            ],
            exclusive_preferred_pd=True,
        )
        states = {"isp1": _down_state("isp1"), "isp2": _up_state("isp2")}

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["is_down"] is True
        assert result["isp2"]["preferred_lifetime"] > 0  # now the only UP uplink = primary

    def test_three_uplinks_only_primary_gets_positive(self, tmp_path):
        cfg = make_config(
            networks=[make_network("lan", "eth1")],
            uplinks=[
                make_uplink("isp1", "eth0", index=0, ipv6_pd=True),
                make_uplink("isp2", "eth3", index=1, ipv6_pd=True),
                make_uplink("isp3", "eth4", index=2, ipv6_pd=True),
            ],
            exclusive_preferred_pd=True,
        )
        states = {
            "isp1": _up_state("isp1"),
            "isp2": _up_state("isp2"),
            "isp3": _up_state("isp3"),
        }

        result = _run_regenerate_all(cfg, states, tmp_path)

        assert result["isp1"]["preferred_lifetime"] > 0
        assert result["isp2"]["preferred_lifetime"] == 0
        assert result["isp3"]["preferred_lifetime"] == 0
