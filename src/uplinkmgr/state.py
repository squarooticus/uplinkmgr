"""Runtime state file reading."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class IPv4State:
    gateway: str


@dataclass
class IPv6GwState:
    gateway: str
    nd1_lifetime: int   # 0 means infinite
    timestamp: int      # Unix epoch when written

    def remaining_lifetime(self, now: Optional[int] = None) -> int:
        if self.nd1_lifetime == 0:
            return 0  # infinite — caller must handle 0 as special
        if now is None:
            now = int(time.time())
        return max(0, self.nd1_lifetime - (now - self.timestamp))


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
    try:
        gw = path.read_text().strip()
        return IPv4State(gateway=gw) if gw else None
    except OSError:
        return None


def read_ipv6gw_state(state_dir: str, uplink_name: str) -> Optional[IPv6GwState]:
    path = Path(state_dir) / f"{uplink_name}.ipv6gw.state"
    return _read_kv_state(path, IPv6GwState, {
        "gateway": str,
        "nd1_lifetime": int,
        "timestamp": int,
    })


def read_ipv6pd_state(state_dir: str, uplink_name: str) -> Optional[IPv6PdState]:
    path = Path(state_dir) / f"{uplink_name}.ipv6pd.state"
    return _read_kv_state(path, IPv6PdState, {
        "delegated_prefix": str,
        "delegated_length": int,
        "vltime": int,
        "pltime": int,
        "timestamp": int,
    })


def _read_kv_state(path: Path, cls, field_types: dict):
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
        kwargs = {k: field_types[k](kv[k]) for k in field_types}
        return cls(**kwargs)
    except (KeyError, ValueError):
        return None
