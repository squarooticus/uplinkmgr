"""Uplink probe logic."""

from __future__ import annotations

import subprocess
from typing import Sequence


def probe_ipv4(wan_iface: str, hosts: Sequence[str], count: int) -> bool:
    """Return True if any ping -c 1 to any host succeeds (up to count attempts per host)."""
    for host in hosts:
        for _ in range(count):
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", "-n", "-q", "-I", wan_iface, host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return True
    return False


def probe_ipv6(wan_iface: str, hosts: Sequence[str], count: int) -> bool:
    """Return True if any ping6 -c 1 to any host succeeds (up to count attempts per host)."""
    for host in hosts:
        for _ in range(count):
            result = subprocess.run(
                ["ping6", "-c", "1", "-W", "2", "-n", "-q", "-I", wan_iface, host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                return True
    return False


def ipv6_default_route_exists(table_num: int, wan_iface: str) -> bool:
    """Return True if the per-uplink table has an IPv6 default route."""
    result = subprocess.run(
        ["ip", "-6", "route", "show", "table", str(table_num), "default",
         "dev", wan_iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return bool(result.stdout.strip())
