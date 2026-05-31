"""Tests for statemachine.py — per-uplink hysteresis state machine."""

import pytest
from uplinkmgr.statemachine import LinkState, UplinkState, update


def make_state(name: str = "isp") -> UplinkState:
    return UplinkState(name=name)


def test_initial_state():
    st = make_state()
    assert st.ipv4 == LinkState.UP
    assert st.ipv6 == LinkState.UP
    assert st.ipv4_consecutive_failures == 0
    assert st.ipv4_consecutive_successes == 0


def test_stays_up_below_failure_threshold():
    st = make_state()
    threshold = 3
    for _ in range(threshold - 1):
        changed4, changed6 = update(st, False, False, True, threshold, threshold)
        assert changed4 is False
        assert changed6 is False
    assert st.ipv4 == LinkState.UP
    assert st.ipv6 == LinkState.UP


def test_transitions_up_to_down_at_failure_threshold():
    st = make_state()
    threshold = 3
    for _ in range(threshold - 1):
        update(st, False, False, True, threshold, threshold)
    changed4, changed6 = update(st, False, False, True, threshold, threshold)
    assert changed4 is True
    assert changed6 is True
    assert st.ipv4 == LinkState.DOWN
    assert st.ipv6 == LinkState.DOWN


def test_stays_down_below_recovery_threshold():
    st = make_state()
    threshold = 3
    for _ in range(threshold):
        update(st, False, False, True, threshold, threshold)
    assert st.ipv4 == LinkState.DOWN

    for _ in range(threshold - 1):
        changed4, _ = update(st, True, True, True, threshold, threshold)
        assert changed4 is False
    assert st.ipv4 == LinkState.DOWN


def test_transitions_down_to_up_at_recovery_threshold():
    st = make_state()
    threshold = 3
    for _ in range(threshold):
        update(st, False, False, True, threshold, threshold)
    assert st.ipv4 == LinkState.DOWN

    for _ in range(threshold - 1):
        update(st, True, True, True, threshold, threshold)
    changed4, changed6 = update(st, True, True, True, threshold, threshold)
    assert changed4 is True
    assert changed6 is True
    assert st.ipv4 == LinkState.UP
    assert st.ipv6 == LinkState.UP


def test_counters_reset_on_direction_change():
    st = make_state()
    threshold = 3
    # 2 failures accumulate
    update(st, False, False, True, threshold, threshold)
    update(st, False, False, True, threshold, threshold)
    assert st.ipv4_consecutive_failures == 2
    # 1 success resets failure counter and starts success counter
    update(st, True, True, True, threshold, threshold)
    assert st.ipv4_consecutive_failures == 0
    assert st.ipv4_consecutive_successes == 1
    # 2 more failures won't trip the threshold (counter was reset)
    update(st, False, False, True, threshold, threshold)
    update(st, False, False, True, threshold, threshold)
    assert st.ipv4 == LinkState.UP


def test_ipv6_disabled_never_changes():
    st = make_state()
    threshold = 3
    for _ in range(threshold * 2):
        _, changed6 = update(st, True, False, False, threshold, threshold)
        assert changed6 is False
    assert st.ipv6 == LinkState.UP


def test_ipv4_and_ipv6_track_independently():
    st = make_state()
    threshold = 3
    for _ in range(threshold):
        update(st, False, True, True, threshold, threshold)
    assert st.ipv4 == LinkState.DOWN
    assert st.ipv6 == LinkState.UP


def test_threshold_of_one():
    st = make_state()
    changed4, _ = update(st, False, True, True, 1, 1)
    assert changed4 is True
    assert st.ipv4 == LinkState.DOWN

    changed4, _ = update(st, True, True, True, 1, 1)
    assert changed4 is True
    assert st.ipv4 == LinkState.UP


def test_failure_counter_resets_after_transition():
    st = make_state()
    threshold = 3
    for _ in range(threshold):
        update(st, False, False, True, threshold, threshold)
    assert st.ipv4 == LinkState.DOWN
    assert st.ipv4_consecutive_failures == 0  # reset after transition
