"""Network namespace and veth helpers for e2e tests.

All operations delegate to iproute2 commands so the test process itself
never needs to change namespaces.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

_log_dir: Optional[Path] = None


def set_log_dir(path: Path) -> None:
    global _log_dir
    _log_dir = path


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), check=check,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if _log_dir is not None:
        with open(str(_log_dir / "netns.log"), "a") as f:
            print(f"+ {' '.join(args)} -> {result.returncode}", file=f)
            if result.stdout:
                f.write(result.stdout.decode(errors="replace"))
            if result.stderr:
                f.write(result.stderr.decode(errors="replace"))
    return result


def create_ns(name: str) -> None:
    _run("ip", "netns", "add", name)


def delete_ns(name: str) -> None:
    _run("ip", "netns", "delete", name, check=False)


def add_veth(name: str, peer: str,
             ns: str | None = None, peer_ns: str | None = None) -> None:
    """Create a veth pair, optionally placing each end in a namespace."""
    _run("ip", "link", "add", name, "type", "veth", "peer", "name", peer)
    if ns:
        _run("ip", "link", "set", name, "netns", ns)
    if peer_ns:
        _run("ip", "link", "set", peer, "netns", peer_ns)


def add_macvlan(ns: str, name: str, parent: str) -> None:
    """Create a macvlan interface over parent inside ns."""
    ip_in(ns, "link", "add", name, "link", parent, "type", "macvlan")


def ip_in(ns: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run an ip command inside a network namespace."""
    return _run("ip", "netns", "exec", ns, "ip", *args, check=check)


def run_in(ns: str, *cmd: str, check: bool = True,
           **kwargs) -> subprocess.CompletedProcess:
    """Run an arbitrary command inside a network namespace."""
    full = ["ip", "netns", "exec", ns] + list(cmd)
    return subprocess.run(full, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          **kwargs)


def link_up(ns: str, iface: str) -> None:
    ip_in(ns, "link", "set", iface, "up")


def add_addr(ns: str, iface: str, cidr: str) -> None:
    ip_in(ns, "addr", "add", cidr, "dev", iface)


def add_route(ns: str, dest: str, via: str, dev: str | None = None) -> None:
    cmd = ["route", "add", dest, "via", via]
    if dev:
        cmd += ["dev", dev]
    ip_in(ns, *cmd)


def route_show(ns: str, table: str = "main") -> str:
    r = ip_in(ns, "route", "show", "table", table)
    return r.stdout.decode()


def rule_show(ns: str, v6: bool = False) -> str:
    cmd = ["-6", "rule", "show"] if v6 else ["rule", "show"]
    r = ip_in(ns, *cmd)
    return r.stdout.decode()


def addr_show(ns: str, iface: str) -> str:
    r = ip_in(ns, "addr", "show", "dev", iface)
    return r.stdout.decode()


def wait_for_file(path: str, timeout: float = 15.0, poll: float = 0.25) -> bool:
    """Return True when path exists, False on timeout."""
    import os
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(poll)
    return False


def wait_for_route(ns: str, table: str, timeout: float = 20.0, poll: float = 0.5) -> bool:
    """Return True when the table contains at least one default route."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = ip_in(ns, "route", "show", "table", table, check=False)
        if r.returncode == 0 and "default" in r.stdout.decode():
            return True
        time.sleep(poll)
    return False


def wait_for_route_gone(ns: str, table: str, dev: str,
                         timeout: float = 30.0, poll: float = 0.5) -> bool:
    """Return True when no default route via dev remains in table."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = ip_in(ns, "route", "show", "table", table, "default", "dev", dev, check=False)
        if not r.stdout.strip():
            return True
        time.sleep(poll)
    return False


def wait_for_slaac(ns: str, iface: str, prefix: str,
                    timeout: float = 30.0, poll: float = 0.5) -> bool:
    """Return True when iface has a global address in prefix."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = addr_show(ns, iface)
        if "scope global" in out and prefix.split("::")[0].lower() in out.lower():
            return True
        time.sleep(poll)
    return False
