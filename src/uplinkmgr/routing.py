"""Routing table and ip rule management."""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

from . import procrun

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy rules (global — installed at daemon startup, removed on stop)
# ---------------------------------------------------------------------------

def add_ipv4_policy_rules(internal_traffic_priority: int, fwd_to_wan_priority: int,
                           ipv4_table: int) -> None:
    _run(["ip", "rule", "add",
          "lookup", "main", "suppress_prefixlength", "0",
          "priority", str(internal_traffic_priority)])
    _run(["ip", "rule", "add",
          "lookup", str(ipv4_table),
          "priority", str(fwd_to_wan_priority)])


def del_ipv4_policy_rules(internal_traffic_priority: int, fwd_to_wan_priority: int) -> None:
    _run_del(["ip", "rule", "del", "priority", str(internal_traffic_priority)])
    _run_del(["ip", "rule", "del", "priority", str(fwd_to_wan_priority)])


def add_ipv6_policy_rule(internal_traffic_priority: int) -> None:
    _run(["ip", "-6", "rule", "add",
          "lookup", "main", "suppress_prefixlength", "0",
          "priority", str(internal_traffic_priority)])


def del_ipv6_policy_rule(internal_traffic_priority: int) -> None:
    _run_del(["ip", "-6", "rule", "del", "priority", str(internal_traffic_priority)])


# ---------------------------------------------------------------------------
# IPv4 routes (shared uplinkmgr table)
# ---------------------------------------------------------------------------

def replace_ipv4_route(gateway: str, iface: str, metric: int, table: int) -> None:
    _run(["ip", "route", "replace", "default",
          "via", gateway, "dev", iface, "metric", str(metric),
          "table", str(table)])


def del_ipv4_route(iface: str, table: int) -> None:
    _run_del(["ip", "route", "del", "default",
              "dev", iface, "table", str(table)])


# ---------------------------------------------------------------------------
# IPv6 routes (per-uplink table)
# ---------------------------------------------------------------------------

def replace_ipv6_route(gateway: str, iface: str, table: int,
                        lifetime: int, remaining: int) -> None:
    cmd = ["ip", "-6", "route", "replace", "default",
           "via", gateway, "dev", iface, "table", str(table)]
    if lifetime != 0:
        cmd += ["expires", str(remaining)]
    _run(cmd)


def del_ipv6_route(iface: str, table: int) -> None:
    _run_del(["ip", "-6", "route", "del", "default",
              "dev", iface, "table", str(table)])


# ---------------------------------------------------------------------------
# IPv6 rules
# ---------------------------------------------------------------------------

def add_ipv6_lo_to_uplink_rule(prefix: str, table: int, priority: int) -> None:
    _run(["ip", "-6", "rule", "add",
          "from", prefix, "iif", "lo",
          "lookup", str(table), "priority", str(priority)])


def add_ipv6_fwd_to_uplink_rule(mv: str, table: int, priority: int,
                                  prefix: Optional[str] = None) -> None:
    cmd = ["ip", "-6", "rule", "add"]
    if prefix is not None:
        cmd += ["from", prefix]
    cmd += ["iif", mv, "lookup", str(table), "priority", str(priority)]
    _run(cmd)


def add_ipv6_reject_wrong_pd_src_rule(mv: str, priority: int) -> None:
    _run(["ip", "-6", "rule", "add",
          "iif", mv, "prohibit", "priority", str(priority)])


def add_ipv4_lo_to_uplink_rule(addr: str, table: int, priority: int) -> None:
    _run(["ip", "rule", "add",
          "from", addr,
          "lookup", str(table), "priority", str(priority)])


def del_ipv4_rule(priority: int) -> None:
    _run_del(["ip", "rule", "del", "priority", str(priority)])


def del_ipv6_rule(priority: int) -> None:
    _run_del(["ip", "-6", "rule", "del", "priority", str(priority)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> None:
    procrun.log_command(log, cmd)
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode().strip()
        log.error("command failed: %s: %s", procrun.format_command(cmd), err)


def _run_del(cmd: list[str]) -> None:
    procrun.log_command(log, cmd)
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode().strip()
        log.warning("delete command failed (may already be absent): %s: %s",
                    procrun.format_command(cmd), err)
