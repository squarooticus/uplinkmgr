"""Per-uplink state machine (IPv4 and IPv6 tracked independently)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LinkState(Enum):
    UP = "up"
    DOWN = "down"


@dataclass
class UplinkState:
    name: str
    ipv4: LinkState = LinkState.UP
    ipv6: LinkState = LinkState.UP
    ipv4_consecutive_failures: int = 0
    ipv4_consecutive_successes: int = 0
    ipv6_consecutive_failures: int = 0
    ipv6_consecutive_successes: int = 0


def update(
    state: UplinkState,
    ipv4_ok: bool,
    ipv6_ok: bool,
    ipv6_enabled: bool,
    failure_threshold: int,
    recovery_threshold: int,
) -> tuple[bool, bool]:
    """Update state machine counters and return (ipv4_changed, ipv6_changed)."""
    ipv4_changed = _update_link(
        state, "ipv4", ipv4_ok, failure_threshold, recovery_threshold
    )
    ipv6_changed = False
    if ipv6_enabled:
        ipv6_changed = _update_link(
            state, "ipv6", ipv6_ok, failure_threshold, recovery_threshold
        )
    return ipv4_changed, ipv6_changed


def _update_link(
    state: UplinkState,
    proto: str,  # "ipv4" or "ipv6"
    ok: bool,
    failure_threshold: int,
    recovery_threshold: int,
) -> bool:
    current: LinkState = getattr(state, proto)
    failures_attr = f"{proto}_consecutive_failures"
    successes_attr = f"{proto}_consecutive_successes"

    if ok:
        setattr(state, failures_attr, 0)
        successes = getattr(state, successes_attr) + 1
        setattr(state, successes_attr, successes)
        if current == LinkState.DOWN and successes >= recovery_threshold:
            setattr(state, proto, LinkState.UP)
            setattr(state, successes_attr, 0)
            return True
    else:
        setattr(state, successes_attr, 0)
        failures = getattr(state, failures_attr) + 1
        setattr(state, failures_attr, failures)
        if current == LinkState.UP and failures >= failure_threshold:
            setattr(state, proto, LinkState.DOWN)
            setattr(state, failures_attr, 0)
            return True

    return False
