"""Runtime state file reading."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class IPv4State:
    gateway: str
    address: str   # WAN IP assigned by dhcpcd


@dataclass
class IPv6RaState:
    gateway: str
    nd1_lifetime: int   # 0 means infinite
    timestamp: int      # Unix epoch when written
    address: str = ""   # SLAAC address or "" (managed) — kept for compat, not used for routing
    prefix: str = ""    # RA prefix address (e.g. "2001:db8:2::")
    plen: int = 0       # RA prefix length

    def remaining_lifetime(self, now: Optional[int] = None) -> int:
        if self.nd1_lifetime == 0:
            return 0  # infinite — caller must handle 0 as special
        if now is None:
            now = int(time.time())
        return max(0, self.nd1_lifetime - (now - self.timestamp))


@dataclass
class IPv6NaState:
    address: str


@dataclass
class IPv6PdState:
    delegated_prefix: str
    delegated_length: int
    vltime: int
    pltime: int
    timestamp: int

    def remaining_vltime(self, now: Optional[int] = None) -> int:
        if now is None:
            now = int(time.time())
        return max(0, self.vltime - (now - self.timestamp))

    def remaining_pltime(self, now: Optional[int] = None) -> int:
        if now is None:
            now = int(time.time())
        return max(0, self.pltime - (now - self.timestamp))


def read_ipv4_state(state_dir: str, uplink_name: str) -> Optional[IPv4State]:
    path = Path(state_dir) / f"{uplink_name}.ipv4.state"
    return _read_kv_state(path, IPv4State, {"gateway": str, "address": str})


def read_ipv6ra_state(state_dir: str, uplink_name: str) -> Optional[IPv6RaState]:
    path = Path(state_dir) / f"{uplink_name}.ipv6ra.state"
    return _read_kv_state(path, IPv6RaState, {
        "gateway": str,
        "nd1_lifetime": int,
        "timestamp": int,
        "address": str,
        "prefix": str,
        "plen": int,
    }, defaults={"address": "", "prefix": "", "plen": 0})


def read_ipv6na_state(state_dir: str, uplink_name: str) -> Optional[IPv6NaState]:
    path = Path(state_dir) / f"{uplink_name}.ipv6na.state"
    return _read_kv_state(path, IPv6NaState, {"address": str})


def read_ipv6pd_state(state_dir: str, uplink_name: str) -> Optional[IPv6PdState]:
    path = Path(state_dir) / f"{uplink_name}.ipv6pd.state"
    return _read_kv_state(path, IPv6PdState, {
        "delegated_prefix": str,
        "delegated_length": int,
        "vltime": int,
        "pltime": int,
        "timestamp": int,
    })


def _read_kv_state(path: Path, cls, field_types: dict,
                   defaults: Optional[dict] = None):
    try:
        text = path.read_text()
    except OSError:
        return None
    kv = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    try:
        kwargs = {}
        for k, t in field_types.items():
            if k in kv:
                kwargs[k] = t(kv[k])
            elif defaults is not None and k in defaults:
                kwargs[k] = defaults[k]
            else:
                raise KeyError(k)
        return cls(**kwargs)
    except (KeyError, ValueError):
        return None
