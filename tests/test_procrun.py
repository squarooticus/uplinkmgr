"""Tests for procrun.py — copy-pasteable command formatting and DEBUG logging."""

from __future__ import annotations

import logging
from unittest.mock import patch

from uplinkmgr.procrun import format_command, log_command


# --- format_command ---

def test_no_env_simple_args_unquoted():
    assert format_command(["ip", "route", "show"]) == "ip route show"


def test_arg_with_space_gets_single_quoted():
    assert format_command(["echo", "hello world"]) == "echo 'hello world'"


def test_arg_with_shell_metacharacters_gets_quoted():
    out = format_command(["sh", "-c", "echo $HOME; rm -rf /"])
    assert out == "sh -c 'echo $HOME; rm -rf /'"


def test_empty_env_dict_same_as_none():
    assert format_command(["ip", "a"], env={}) == format_command(["ip", "a"], env=None)


def test_env_override_rendered_before_command():
    out = format_command(["cmd", "arg"], env={"FOO": "bar"})
    assert out == "FOO=bar cmd arg"


def test_env_value_with_space_gets_quoted():
    out = format_command(["cmd"], env={"FOO": "bar baz"})
    assert out == "FOO='bar baz' cmd"


def test_multiple_env_vars_all_rendered():
    out = format_command(["cmd"], env={"A": "1", "B": "2"})
    assert out == "A=1 B=2 cmd"


def test_non_string_args_stringified():
    assert format_command(["ip", "rule", "add", "priority", 100]) == "ip rule add priority 100"


# --- log_command ---
#
# caplog.at_level() forces the target logger's *level* to the given value
# for its duration, which would defeat these tests (they need the logger
# left at a level *of the test's choosing*, not forced to DEBUG just to
# capture output). So level and capture are driven independently: setLevel()
# picks the level under test, and log.debug is mocked to observe whether
# log_command actually emitted anything, without caplog interfering.

def _logger_at(level: int, name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(level)
    return log


def test_logs_at_debug_when_debug_enabled():
    log = _logger_at(logging.DEBUG, "test_procrun.enabled")
    with patch.object(log, "debug") as debug:
        log_command(log, ["ip", "route", "show"])
    debug.assert_called_once_with("+ %s", "ip route show")


def test_does_not_log_when_debug_disabled():
    log = _logger_at(logging.INFO, "test_procrun.disabled")
    with patch.object(log, "debug") as debug:
        log_command(log, ["ip", "route", "show"])
    debug.assert_not_called()


def test_logs_env_overrides_when_present():
    log = _logger_at(logging.DEBUG, "test_procrun.env")
    with patch.object(log, "debug") as debug:
        log_command(log, ["cmd"], env={"FOO": "bar"})
    debug.assert_called_once_with("+ %s", "FOO=bar cmd")
