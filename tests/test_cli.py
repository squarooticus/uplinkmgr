"""Tests for bin/uplinkmgr's CLI argument parsing and log formatting.

bin/uplinkmgr is a script (no .py extension, not on sys.path as a package),
so it's loaded directly via importlib rather than imported normally.
"""

from __future__ import annotations

import importlib.util
import logging
from importlib.machinery import SourceFileLoader
from pathlib import Path

_CLI_PATH = Path(__file__).parent.parent / "bin" / "uplinkmgr"

_loader = SourceFileLoader("uplinkmgr_cli", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
cli = importlib.util.module_from_spec(_spec)
_loader.exec_module(cli)


class TestLogFormat:
    def test_default_format_includes_timestamp_and_process_name(self):
        fmt = cli.log_format(clean=False)
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "hello", None, None,
        )
        line = logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S").format(record)
        assert "uplinkmgr" in line
        assert "INFO" in line
        assert "hello" in line
        assert line.split(" ")[0].count("-") == 2  # leading ISO date

    def test_clean_format_omits_timestamp_and_process_name(self):
        fmt = cli.log_format(clean=True)
        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "hello", None, None,
        )
        line = logging.Formatter(fmt).format(record)
        assert line == "WARNING hello"
        assert "uplinkmgr" not in line


class TestParseArgs:
    def test_log_clean_defaults_false(self):
        args = cli.parse_args([])
        assert args.log_clean is False

    def test_log_clean_flag_sets_true(self):
        args = cli.parse_args(["--log-clean"])
        assert args.log_clean is True

    def test_hooks_dirs_default_to_module_constants(self):
        args = cli.parse_args([])
        assert args.hooks_system_dir == cli.hooks_mod.HOOKS_SYSTEM_DIR
        assert args.hooks_user_dir == cli.hooks_mod.HOOKS_USER_DIR

    def test_hooks_dirs_overridable(self):
        args = cli.parse_args(["--hooks-system-dir", "/tmp/sys",
                                "--hooks-user-dir", "/tmp/user"])
        assert args.hooks_system_dir == "/tmp/sys"
        assert args.hooks_user_dir == "/tmp/user"
