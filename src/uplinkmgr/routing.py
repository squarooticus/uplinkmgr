"""IPv4 route management in the main routing table."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)


def add_ipv4_default(gateway: str, wan_iface: str, metric: int) -> None:
    _run(["ip", "route", "replace", "default",
          "via", gateway, "dev", wan_iface, "metric", str(metric)])


def del_ipv4_default(gateway: str, wan_iface: str, metric: int) -> None:
    result = subprocess.run(
        ["ip", "route", "del", "default",
         "via", gateway, "dev", wan_iface, "metric", str(metric)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        err = result.stderr.decode().strip()
        log.warning("ip route del default failed (may already be gone): %s", err)


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode().strip()
        log.error("command failed: %s: %s", " ".join(cmd), err)
