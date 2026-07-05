"""Uplink probe logic."""

from __future__ import annotations

import logging
import subprocess
from typing import Optional, Sequence

from . import procrun

log = logging.getLogger(__name__)


def probe_ipv4(wan_iface: str, hosts: Sequence[str], count: int) -> bool:
    """Return True if any ping -c 1 to any host succeeds (up to count attempts per host)."""
    for host in hosts:
        for _ in range(count):
            cmd = ["ping", "-c", "1", "-W", "2", "-n", "-q", "-I", wan_iface, host]
            procrun.log_command(log, cmd)
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return True
    return False


def probe_ipv6(wan_iface: str, hosts: Sequence[str], count: int,
                src_addr: Optional[str] = None) -> bool:
    """Return True if any ping6 -c 1 to any host succeeds (up to count attempts per host).

    When src_addr is given, a second -I binds the probe to that source
    address (in addition to the interface bind), so route lookup doesn't
    depend on the main table to bootstrap source address selection.
    """
    for host in hosts:
        for _ in range(count):
            cmd = ["ping6", "-c", "1", "-W", "2", "-n", "-q", "-I", wan_iface]
            if src_addr:
                cmd += ["-I", src_addr]
            cmd.append(host)
            procrun.log_command(log, cmd)
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                return True
    return False
