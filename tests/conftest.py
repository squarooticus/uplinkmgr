"""Shared fixtures and helper functions for the uplinkmgr test suite."""

import pytest

from uplinkmgr.config import (
    Config, MonitorConfig, NetworkConfig, UplinkConfig,
    DEFAULT_ROUTING_TABLE_START, DEFAULT_RULE_PRIORITY_START,
    DEFAULT_RADVD_MIN_RESTART_INTERVAL, DEFAULT_HOOK_TIMEOUT,
)


def make_monitor(**kwargs) -> MonitorConfig:
    return MonitorConfig(
        interval=kwargs.get("interval", 10),
        failure_threshold=kwargs.get("failure_threshold", 3),
        recovery_threshold=kwargs.get("recovery_threshold", 3),
        v4_hosts=kwargs.get("v4_hosts", ["8.8.8.8"]),
        v6_hosts=kwargs.get("v6_hosts", ["2001:4860:4860::8888"]),
        ping_count=kwargs.get("ping_count", 3),
    )


def make_network(name: str = "lan", interface: str = "eth1") -> NetworkConfig:
    return NetworkConfig(name=name, interface=interface)


def make_uplink(name: str = "isp", interface: str = "eth0", index: int = 0,
                **kwargs) -> UplinkConfig:
    return UplinkConfig(
        name=name,
        interface=interface,
        ipv6_pd=kwargs.get("ipv6_pd", False),
        ipv6_pd_hint=kwargs.get("ipv6_pd_hint", 56),
        ia_na=kwargs.get("ia_na", False),
        metric=kwargs.get("metric", 100 * (index + 1)),
        index=index,
    )


def make_config(networks=None, uplinks=None, **kwargs) -> Config:
    return Config(
        routing_table_start=kwargs.get("routing_table_start", DEFAULT_ROUTING_TABLE_START),
        rule_priority_start=kwargs.get("rule_priority_start", DEFAULT_RULE_PRIORITY_START),
        reject_wrong_pd_src=kwargs.get("reject_wrong_pd_src", False),
        exclusive_preferred_pd=kwargs.get("exclusive_preferred_pd", False),
        radvd_min_restart_interval=kwargs.get("radvd_min_restart_interval",
                                               DEFAULT_RADVD_MIN_RESTART_INTERVAL),
        hook_timeout=kwargs.get("hook_timeout", DEFAULT_HOOK_TIMEOUT),
        monitor=kwargs.get("monitor", make_monitor()),
        networks=networks if networks is not None else [make_network()],
        uplinks=uplinks if uplinks is not None else [make_uplink()],
    )


def write_state(tmp_path, uplink_name: str, state_type: str, fields: dict) -> None:
    """Write a key=value state file to tmp_path for use in read_*_state tests."""
    path = tmp_path / f"{uplink_name}.{state_type}.state"
    path.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))


@pytest.fixture
def minimal_cfg() -> Config:
    return make_config()


@pytest.fixture
def dual_uplink_cfg() -> Config:
    return make_config(
        networks=[make_network("lan", "eth1"), make_network("dmz", "eth2")],
        uplinks=[
            make_uplink("isp1", "eth0", index=0, ipv6_pd=True, ia_na=True, metric=100),
            make_uplink("isp2", "eth3", index=1, metric=200),
        ],
    )
