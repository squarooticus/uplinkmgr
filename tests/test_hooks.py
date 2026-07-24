"""Tests for hooks.py — discovery/shadow-resolution/ordering and execution."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from uplinkmgr import hooks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _script(directory: Path, name: str, body: str, mode: int = 0o755) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    os.chmod(path, mode)
    return path


def _dirs(tmp_path) -> tuple[Path, Path]:
    system_dir = tmp_path / "system"
    user_dir = tmp_path / "user"
    system_dir.mkdir()
    user_dir.mkdir()
    return system_dir, user_dir


# ---------------------------------------------------------------------------
# _stem
# ---------------------------------------------------------------------------

class TestStem:
    def test_two_digit_prefix_stripped(self):
        assert hooks._stem("50-myhook") == "myhook"

    def test_three_digit_prefix_stripped(self):
        assert hooks._stem("007-cleanup.sh") == "cleanup.sh"

    def test_single_digit_prefix_stripped(self):
        assert hooks._stem("5-foo") == "foo"

    def test_no_digit_prefix_unchanged(self):
        assert hooks._stem("myhook.sh") == "myhook.sh"

    def test_digit_not_followed_by_hyphen_unchanged(self):
        assert hooks._stem("50myhook") == "50myhook"

    def test_only_leading_prefix_stripped_once(self):
        assert hooks._stem("10-20-myhook") == "20-myhook"


# ---------------------------------------------------------------------------
# resolve_hook_scripts — aggregation, ordering, shadowing
# ---------------------------------------------------------------------------

class TestResolveHookScripts:
    def test_missing_directories_return_empty(self, tmp_path):
        assert hooks.resolve_hook_scripts(
            str(tmp_path / "nope1"), str(tmp_path / "nope2")
        ) == []

    def test_aggregated_and_sorted_by_filename_across_both_dirs(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "50-systemhook", ":")
        _script(user_dir, "10-myhook", ":")

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert [p.name for p in result] == ["10-myhook", "50-systemhook"]
        assert result[0].parent == user_dir
        assert result[1].parent == system_dir

    def test_user_entry_shadows_same_stem_system_entry(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "50-myhook", ":")
        _script(user_dir, "05-myhook", ":")

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        # Only the /etc override survives; the system one is fully dropped,
        # and the survivor's own filename ("05-myhook") determines position.
        assert [p.name for p in result] == ["05-myhook"]
        assert result[0].parent == user_dir

    def test_differing_stems_both_survive_and_interleave_by_filename(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "10-a", ":")
        _script(user_dir, "20-b", ":")

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert [p.name for p in result] == ["10-a", "20-b"]

    def test_invalid_override_still_suppresses_system_hook_fail_closed(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "50-myhook", ":")
        # Override present but non-executable -- still shadows per fail-closed
        # semantics, and doesn't itself run either (checked in run_hooks_sync
        # tests below via _is_eligible). Here we only check *resolution*:
        # the survivor list still contains the (currently-ineligible) override,
        # not the system hook.
        _script(user_dir, "50-myhook", ":", mode=0o644)

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert [p.name for p in result] == ["50-myhook"]
        assert result[0].parent == user_dir

    def test_dotfiles_excluded_from_candidates_and_shadowing(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "50-myhook", ":")
        _script(user_dir, ".50-myhook", ":")  # hidden file, same stem -- must not shadow

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert [p.name for p in result] == ["50-myhook"]
        assert result[0].parent == system_dir

    @pytest.mark.parametrize("suffix", ["~", ".bak", ".dpkg-old", ".rpmsave", ".swp"])
    def test_backup_suffixes_excluded(self, tmp_path, suffix):
        system_dir, user_dir = _dirs(tmp_path)
        _script(user_dir, f"10-myhook{suffix}", ":")

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert result == []

    def test_multiple_stems_in_same_user_dir_both_shadow_matching_system_stems(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(system_dir, "01-myhook", ":")
        _script(system_dir, "02-myhook", ":")  # two system files, same stem
        _script(user_dir, "05-myhook", ":")

        result = hooks.resolve_hook_scripts(str(system_dir), str(user_dir))

        assert [p.name for p in result] == ["05-myhook"]


# ---------------------------------------------------------------------------
# _is_eligible
# ---------------------------------------------------------------------------

class TestIsEligible:
    def test_plain_executable_is_eligible(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o755)
        assert hooks._is_eligible(p) is True

    def test_non_executable_rejected(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o644)
        assert hooks._is_eligible(p) is False

    def test_group_writable_rejected(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o775)
        assert hooks._is_eligible(p) is False

    def test_other_writable_rejected(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o757)
        assert hooks._is_eligible(p) is False

    def test_setuid_rejected(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o4755)
        assert hooks._is_eligible(p) is False

    def test_setgid_rejected(self, tmp_path):
        p = _script(tmp_path, "hook", ":", mode=0o2755)
        assert hooks._is_eligible(p) is False

    def test_directory_rejected(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir(mode=0o755)
        assert hooks._is_eligible(d) is False

    def test_missing_file_rejected(self, tmp_path):
        assert hooks._is_eligible(tmp_path / "nonexistent") is False


# ---------------------------------------------------------------------------
# run_hooks_sync — real subprocess execution
# ---------------------------------------------------------------------------

class TestRunHooksSync:
    def test_runs_eligible_scripts_with_argv_and_env(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        marker = tmp_path / "marker"
        _script(user_dir, "10-hook", f'''
printf '%s\\n' "$1" "$2" "$UPLINKMGR_EVENT" "$UPLINKMGR_UPLINK" \
    "$UPLINKMGR_FAMILY" "$UPLINKMGR_STATE_DIR" "$UPLINKMGR_CONFIG_PATH" \
    > "{marker}"
''')
        hooks.run_hooks_sync(
            "wan-down", "isp1", {"family": "ipv4"},
            timeout=5, system_dir=str(system_dir), user_dir=str(user_dir),
            state_dir="/run/uplinkmgr", config_path="/etc/uplinkmgr/uplinkmgr.yaml",
        )

        lines = marker.read_text().splitlines()
        assert lines == [
            "wan-down", "isp1", "wan-down", "isp1", "ipv4",
            "/run/uplinkmgr", "/etc/uplinkmgr/uplinkmgr.yaml",
        ]

    def test_ineligible_script_skipped_no_error(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        _script(user_dir, "10-hook", "exit 0", mode=0o644)  # not executable

        # Must not raise.
        hooks.run_hooks_sync(
            "reload", "", {}, timeout=5,
            system_dir=str(system_dir), user_dir=str(user_dir),
            state_dir="/run/uplinkmgr", config_path="/etc/uplinkmgr/uplinkmgr.yaml",
        )

    def test_nonzero_exit_does_not_block_later_hooks(self, tmp_path, caplog):
        system_dir, user_dir = _dirs(tmp_path)
        marker = tmp_path / "marker"
        _script(user_dir, "10-fails", "exit 1")
        _script(user_dir, "20-ok", f'echo ran > "{marker}"')

        hooks.run_hooks_sync(
            "reload", "", {}, timeout=5,
            system_dir=str(system_dir), user_dir=str(user_dir),
            state_dir="/run/uplinkmgr", config_path="/etc/uplinkmgr/uplinkmgr.yaml",
        )

        assert marker.read_text() == "ran\n"

    def test_slow_script_killed_at_timeout(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        marker = tmp_path / "marker"
        _script(user_dir, "10-slow", f'sleep 5 && echo done > "{marker}"')

        start = time.monotonic()
        hooks.run_hooks_sync(
            "reload", "", {}, timeout=1,
            system_dir=str(system_dir), user_dir=str(user_dir),
            state_dir="/run/uplinkmgr", config_path="/etc/uplinkmgr/uplinkmgr.yaml",
        )
        elapsed = time.monotonic() - start

        assert elapsed < 4  # killed well before the 5s sleep would finish
        assert not marker.exists()  # script never reached the echo

    def test_env_extra_none_or_empty_omitted(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        marker = tmp_path / "marker"
        _script(user_dir, "10-hook", f'''
if [ -z "${{UPLINKMGR_GATEWAY+x}}" ]; then echo unset > "{marker}"; else echo set > "{marker}"; fi
''')
        hooks.run_hooks_sync(
            "wan-down", "isp1", {"gateway": None, "address": ""}, timeout=5,
            system_dir=str(system_dir), user_dir=str(user_dir),
            state_dir="/run/uplinkmgr", config_path="/etc/uplinkmgr/uplinkmgr.yaml",
        )
        assert marker.read_text().strip() == "unset"


# ---------------------------------------------------------------------------
# HookRunner — async, serialized execution
# ---------------------------------------------------------------------------

class TestHookRunner:
    def test_fire_returns_immediately(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        marker = tmp_path / "marker"
        _script(user_dir, "10-slow", f'sleep 1 && echo done > "{marker}"')

        runner = hooks.HookRunner(
            state_dir=str(tmp_path), config_path="cfg.yaml",
            system_dir=str(system_dir), user_dir=str(user_dir), timeout=5,
        )
        start = time.monotonic()
        runner.fire("reload")
        elapsed = time.monotonic() - start

        assert elapsed < 0.5  # returned long before the 1s sleep completes
        assert not marker.exists()

        runner.shutdown(timeout=5)
        assert marker.read_text().strip() == "done"

    def test_queued_events_run_serially_in_order(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        order_log = tmp_path / "order.log"
        _script(user_dir, "10-a", f'echo a >> "{order_log}"')
        _script(user_dir, "20-b", f'echo b >> "{order_log}"')

        runner = hooks.HookRunner(
            state_dir=str(tmp_path), config_path="cfg.yaml",
            system_dir=str(system_dir), user_dir=str(user_dir), timeout=5,
        )
        runner.fire("wan-down", uplink="x")
        runner.fire("wan-up", uplink="x")
        runner.shutdown(timeout=5)

        # Each fire() re-runs both scripts (10-a then 20-b), twice total.
        assert order_log.read_text().splitlines() == ["a", "b", "a", "b"]

    def test_shutdown_is_idempotent_enough_for_double_call_not_required(self, tmp_path):
        system_dir, user_dir = _dirs(tmp_path)
        runner = hooks.HookRunner(
            state_dir=str(tmp_path), config_path="cfg.yaml",
            system_dir=str(system_dir), user_dir=str(user_dir), timeout=5,
        )
        runner.shutdown(timeout=5)
        assert not runner._thread.is_alive()
