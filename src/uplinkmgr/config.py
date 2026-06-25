"""YAML config loading, defaulting, and validation."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("error: PyYAML is required (python3-yaml)", file=sys.stderr)
    sys.exit(1)


DEFAULT_CONFIG_PATH = "/etc/uplinkmgr/uplinkmgr.yaml"
DEFAULT_ROUTING_TABLE_START = 160
DEFAULT_RULE_PRIORITY_START = 29000
DEFAULT_RADVD_MIN_RESTART_INTERVAL = 60
DEFAULT_MONITOR_INTERVAL = 10
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RECOVERY_THRESHOLD = 3
DEFAULT_V4_HOSTS = ["8.8.8.8", "1.1.1.1"]
DEFAULT_V6_HOSTS = ["2001:4860:4860::8888", "2606:4700:4700::1111"]
DEFAULT_METRIC_MULTIPLIER = 100
DEFAULT_IPV6_PD_HINT = 56


@dataclass
class MonitorConfig:
    interval: int
    failure_threshold: int
    recovery_threshold: int
    v4_hosts: list[str]
    v6_hosts: list[str]
    ping_count: int


@dataclass
class NetworkConfig:
    name: str
    interface: str


@dataclass
class UplinkConfig:
    name: str
    interface: str
    ipv6_pd: bool
    ipv6_pd_hint: int
    ia_na: bool
    metric: int
    index: int  # 0-based position in uplinks list


@dataclass
class Config:
    routing_table_start: int
    rule_priority_start: int
    reject_wrong_pd_src: bool
    exclusive_preferred_pd: bool
    radvd_min_restart_interval: int
    monitor: MonitorConfig
    networks: list[NetworkConfig]
    uplinks: list[UplinkConfig]


_NAME_RE = re.compile(r'^[a-zA-Z0-9-]+$')
_IFACE_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,14}$')


def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        _die(f"config file not found: {path}")
    except OSError as e:
        _die(f"cannot read config file {path}: {e}")

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        _die(f"config file parse error: {e}")

    if not isinstance(raw, dict) or "uplinkmgr" not in raw:
        _die("config file must have a top-level 'uplinkmgr:' key")

    top = raw["uplinkmgr"]
    if not isinstance(top, dict):
        _die("'uplinkmgr' must be a mapping")

    routing_table_start = int(top.get("routing_table_start", DEFAULT_ROUTING_TABLE_START))
    rule_priority_start = int(top.get("rule_priority_start", DEFAULT_RULE_PRIORITY_START))
    reject_wrong_pd_src = bool(top.get("reject_wrong_pd_src", False))
    exclusive_preferred_pd = bool(top.get("exclusive_preferred_pd", False))
    radvd_min_restart_interval = int(top.get("radvd_min_restart_interval",
                                              DEFAULT_RADVD_MIN_RESTART_INTERVAL))

    if not (1 <= routing_table_start <= 252):
        _die(f"routing_table_start must be 1–252, got {routing_table_start}")

    monitor = _parse_monitor(top.get("monitor", {}))
    networks = _parse_networks(top.get("networks", []))
    uplinks = _parse_uplinks(top.get("uplinks", []), routing_table_start)

    if not networks:
        _die("at least one network must be defined")
    if not uplinks:
        _die("at least one uplink must be defined")

    # 1 shared IPv4 table + len(uplinks) IPv6 per-uplink tables
    table_end = routing_table_start + len(uplinks)
    if table_end > 252:
        _die(
            f"routing_table_start={routing_table_start} with {len(uplinks)} uplinks "
            f"would reach table {table_end} (1 IPv4 + {len(uplinks)} IPv6), exceeding 252"
        )

    return Config(
        routing_table_start=routing_table_start,
        rule_priority_start=rule_priority_start,
        reject_wrong_pd_src=reject_wrong_pd_src,
        exclusive_preferred_pd=exclusive_preferred_pd,
        radvd_min_restart_interval=radvd_min_restart_interval,
        monitor=monitor,
        networks=networks,
        uplinks=uplinks,
    )


def _parse_monitor(raw: object) -> MonitorConfig:
    if not isinstance(raw, dict):
        raw = {}
    return MonitorConfig(
        interval=int(raw.get("interval", DEFAULT_MONITOR_INTERVAL)),
        failure_threshold=int(raw.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD)),
        recovery_threshold=int(raw.get("recovery_threshold", DEFAULT_RECOVERY_THRESHOLD)),
        v4_hosts=list(raw.get("v4_hosts", DEFAULT_V4_HOSTS)),
        v6_hosts=list(raw.get("v6_hosts", DEFAULT_V6_HOSTS)),
        ping_count=int(raw.get("ping_count", 3)),
    )


def _parse_networks(raw: object) -> list[NetworkConfig]:
    if not isinstance(raw, list):
        _die("'networks' must be a list")
    networks = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _die(f"networks[{i}] must be a mapping")
        name = str(entry.get("name", ""))
        iface = str(entry.get("interface", ""))
        if not name:
            _die(f"networks[{i}] missing 'name'")
        if not iface:
            _die(f"networks[{i}] missing 'interface'")
        _validate_iface(iface, f"networks[{i}].interface")
        networks.append(NetworkConfig(name=name, interface=iface))
    return networks


def _parse_uplinks(raw: object, routing_table_start: int) -> list[UplinkConfig]:
    if not isinstance(raw, list):
        _die("'uplinks' must be a list")
    uplinks = []
    seen_names: set[str] = set()
    seen_ifaces: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _die(f"uplinks[{i}] must be a mapping")
        name = str(entry.get("name", ""))
        iface = str(entry.get("interface", ""))
        ipv6_pd = bool(entry.get("ipv6_pd", False))
        ipv6_pd_hint = int(entry.get("ipv6_pd_hint", DEFAULT_IPV6_PD_HINT))
        ia_na = bool(entry.get("ia_na", False))
        metric = int(entry.get("metric", DEFAULT_METRIC_MULTIPLIER * (i + 1)))

        if not name:
            _die(f"uplinks[{i}] missing 'name'")
        if not _NAME_RE.match(name):
            _die(f"uplinks[{i}].name '{name}' must contain only alphanumerics and hyphens")
        if name in seen_names:
            _die(f"duplicate uplink name '{name}'")
        seen_names.add(name)

        if not iface:
            _die(f"uplinks[{i}] missing 'interface'")
        _validate_iface(iface, f"uplinks[{i}].interface")
        if iface in seen_ifaces:
            _die(f"duplicate uplink interface '{iface}'")
        seen_ifaces.add(iface)

        if metric <= 0:
            _die(f"uplinks[{i}].metric must be positive, got {metric}")
        if not (0 < ipv6_pd_hint <= 64):
            _die(f"uplinks[{i}].ipv6_pd_hint must be 1–64, got {ipv6_pd_hint}")

        uplinks.append(UplinkConfig(
            name=name,
            interface=iface,
            ipv6_pd=ipv6_pd,
            ipv6_pd_hint=ipv6_pd_hint,
            ia_na=ia_na,
            metric=metric,
            index=i,
        ))
    return uplinks


def _validate_iface(name: str, ctx: str) -> None:
    if len(name) > 15:
        _die(f"{ctx}: interface name '{name}' exceeds 15 characters")
    if not _IFACE_RE.match(name):
        _die(f"{ctx}: invalid interface name '{name}'")


def _die(msg: str) -> None:
    print(f"uplinkmgr: config error: {msg}", file=sys.stderr)
    sys.exit(1)
