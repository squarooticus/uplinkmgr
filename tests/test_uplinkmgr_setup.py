"""Tests for bin/uplinkmgr-setup's stale radvd unit cleanup.

bin/uplinkmgr-setup is a script (no .py extension, not on sys.path as a
package), so it's loaded directly via importlib rather than imported normally.
"""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import make_config, make_uplink

_SETUP_PATH = Path(__file__).parent.parent / "bin" / "uplinkmgr-setup"

_loader = SourceFileLoader("uplinkmgr_setup_cli", str(_SETUP_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
setup = importlib.util.module_from_spec(_spec)
_loader.exec_module(setup)


def _list_units_output(*units: str) -> bytes:
    lines = [
        f"{unit}   loaded active running   Router advertisement daemon"
        for unit in units
    ]
    return ("\n".join(lines) + "\n" if lines else "").encode()


class TestListRadvdUnits:
    def test_parses_unit_names_from_systemctl_output(self):
        result = MagicMock(stdout=_list_units_output(
            "radvd-uplinkmgr@isp1.service", "radvd-uplinkmgr@isp2.service",
        ))
        with patch("subprocess.run", return_value=result) as run:
            units = setup._list_radvd_units()
        assert units == {"radvd-uplinkmgr@isp1.service", "radvd-uplinkmgr@isp2.service"}
        cmd = run.call_args[0][0]
        assert cmd == ["systemctl", "list-units", "--all", "--plain", "--no-legend", "radvd-uplinkmgr@*"]

    def test_empty_output_returns_empty_set(self):
        result = MagicMock(stdout=_list_units_output())
        with patch("subprocess.run", return_value=result):
            units = setup._list_radvd_units()
        assert units == set()


class TestDisableStaleRadvdUnits:
    def _cfg(self):
        return make_config(uplinks=[
            make_uplink("isp1", "eth0", index=0),
            make_uplink("isp2", "eth3", index=1),
        ])

    def test_stops_and_disables_units_for_removed_uplinks(self):
        cfg = self._cfg()
        with patch.object(setup, "_list_radvd_units", return_value={
            "radvd-uplinkmgr@isp1.service",
            "radvd-uplinkmgr@isp2.service",
            "radvd-uplinkmgr@old-isp.service",
        }):
            with patch.object(setup, "_systemctl") as systemctl:
                setup._disable_stale_radvd_units(cfg, dry_run=False)

        calls = [call.args for call in systemctl.call_args_list]
        assert calls == [
            ("stop", "radvd-uplinkmgr@old-isp.service", False),
            ("disable", "radvd-uplinkmgr@old-isp.service", False),
        ]

    def test_no_stale_units_makes_no_systemctl_calls(self):
        cfg = self._cfg()
        with patch.object(setup, "_list_radvd_units", return_value={
            "radvd-uplinkmgr@isp1.service",
            "radvd-uplinkmgr@isp2.service",
        }):
            with patch.object(setup, "_systemctl") as systemctl:
                setup._disable_stale_radvd_units(cfg, dry_run=False)

        systemctl.assert_not_called()

    def test_passes_dry_run_through_to_systemctl(self):
        cfg = self._cfg()
        with patch.object(setup, "_list_radvd_units", return_value={
            "radvd-uplinkmgr@old-isp.service",
        }):
            with patch.object(setup, "_systemctl") as systemctl:
                setup._disable_stale_radvd_units(cfg, dry_run=True)

        for call in systemctl.call_args_list:
            assert call.args[2] is True

    def test_multiple_stale_units_processed_in_sorted_order(self):
        cfg = self._cfg()
        with patch.object(setup, "_list_radvd_units", return_value={
            "radvd-uplinkmgr@zzz-old.service",
            "radvd-uplinkmgr@aaa-old.service",
        }):
            with patch.object(setup, "_systemctl") as systemctl:
                setup._disable_stale_radvd_units(cfg, dry_run=False)

        stopped_units = [call.args[1] for call in systemctl.call_args_list if call.args[0] == "stop"]
        assert stopped_units == ["radvd-uplinkmgr@aaa-old.service", "radvd-uplinkmgr@zzz-old.service"]
