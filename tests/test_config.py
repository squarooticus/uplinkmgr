"""Tests for config.py — YAML parsing and validation."""

import textwrap
import pytest

from uplinkmgr.config import (
    load,
    DEFAULT_ROUTING_TABLE_START, DEFAULT_RULE_PRIORITY_START,
    DEFAULT_RADVD_MIN_RESTART_INTERVAL, DEFAULT_MONITOR_INTERVAL,
    DEFAULT_FAILURE_THRESHOLD, DEFAULT_RECOVERY_THRESHOLD,
    DEFAULT_METRIC_MULTIPLIER, DEFAULT_IPV6_PD_HINT,
)


def _cfg_file(tmp_path, content: str) -> str:
    p = tmp_path / "uplinkmgr.yaml"
    p.write_text(textwrap.dedent(content))
    return str(p)


MINIMAL = """
    uplinkmgr:
      networks:
        - name: lan
          interface: eth1
      uplinks:
        - name: isp
          interface: eth0
    """


def test_minimal_loads_and_applies_defaults(tmp_path):
    cfg = load(_cfg_file(tmp_path, MINIMAL))

    assert len(cfg.uplinks) == 1
    u = cfg.uplinks[0]
    assert u.name == "isp"
    assert u.interface == "eth0"
    assert u.ipv6_pd is False
    assert u.ia_na is False
    assert u.ipv6_pd_hint == DEFAULT_IPV6_PD_HINT
    assert u.metric == DEFAULT_METRIC_MULTIPLIER * 1
    assert u.index == 0

    assert len(cfg.networks) == 1
    assert cfg.networks[0].name == "lan"
    assert cfg.networks[0].interface == "eth1"

    assert cfg.routing_table_start == DEFAULT_ROUTING_TABLE_START
    assert cfg.rule_priority_start == DEFAULT_RULE_PRIORITY_START
    assert cfg.reject_wrong_pd_src is False
    assert cfg.exclusive_preferred_pd is False
    assert cfg.radvd_min_restart_interval == DEFAULT_RADVD_MIN_RESTART_INTERVAL
    assert cfg.monitor.interval == DEFAULT_MONITOR_INTERVAL
    assert cfg.monitor.failure_threshold == DEFAULT_FAILURE_THRESHOLD
    assert cfg.monitor.recovery_threshold == DEFAULT_RECOVERY_THRESHOLD
    assert cfg.monitor.ping_count == 3


def test_all_options_explicit(tmp_path):
    cfg = load(_cfg_file(tmp_path, """
        uplinkmgr:
          routing_table_start: 100
          rule_priority_start: 20000
          reject_wrong_pd_src: true
          radvd_min_restart_interval: 30
          monitor:
            interval: 5
            failure_threshold: 2
            recovery_threshold: 2
            ping_count: 5
          networks:
            - name: lan
              interface: eth1
          uplinks:
            - name: isp
              interface: eth0
              ipv6_pd: true
              ipv6_pd_hint: 48
              ia_na: true
              metric: 500
        """))

    assert cfg.routing_table_start == 100
    assert cfg.rule_priority_start == 20000
    assert cfg.reject_wrong_pd_src is True
    assert cfg.radvd_min_restart_interval == 30
    assert cfg.monitor.interval == 5
    assert cfg.monitor.failure_threshold == 2
    assert cfg.monitor.recovery_threshold == 2
    assert cfg.monitor.ping_count == 5
    u = cfg.uplinks[0]
    assert u.ipv6_pd is True
    assert u.ia_na is True
    assert u.ipv6_pd_hint == 48
    assert u.metric == 500


def test_exclusive_preferred_pd_parses(tmp_path):
    cfg = load(_cfg_file(tmp_path, """
        uplinkmgr:
          exclusive_preferred_pd: true
          networks:
            - name: lan
              interface: eth1
          uplinks:
            - name: isp
              interface: eth0
        """))
    assert cfg.exclusive_preferred_pd is True


def test_ia_na_without_ipv6_pd(tmp_path):
    cfg = load(_cfg_file(tmp_path, """
        uplinkmgr:
          networks:
            - name: lan
              interface: eth1
          uplinks:
            - name: isp
              interface: eth0
              ia_na: true
        """))
    assert cfg.uplinks[0].ia_na is True
    assert cfg.uplinks[0].ipv6_pd is False


def test_multiple_uplinks_get_sequential_indices_and_default_metrics(tmp_path):
    cfg = load(_cfg_file(tmp_path, """
        uplinkmgr:
          networks:
            - name: lan
              interface: eth1
          uplinks:
            - name: isp1
              interface: eth0
            - name: isp2
              interface: eth3
        """))
    assert cfg.uplinks[0].index == 0
    assert cfg.uplinks[1].index == 1
    assert cfg.uplinks[0].metric == DEFAULT_METRIC_MULTIPLIER * 1
    assert cfg.uplinks[1].metric == DEFAULT_METRIC_MULTIPLIER * 2


def test_ping_count_override(tmp_path):
    cfg = load(_cfg_file(tmp_path, """
        uplinkmgr:
          monitor:
            ping_count: 5
          networks:
            - name: lan
              interface: eth1
          uplinks:
            - name: isp
              interface: eth0
        """))
    assert cfg.monitor.ping_count == 5


# --- Validation errors ---

def test_missing_top_level_key(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, "other: {}\n"))


def test_missing_uplink_name(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - interface: eth0
            """))


def test_missing_uplink_interface(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
            """))


def test_duplicate_uplink_name(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: eth0
                - name: isp
                  interface: eth3
            """))


def test_duplicate_uplink_interface(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp1
                  interface: eth0
                - name: isp2
                  interface: eth0
            """))


def test_interface_name_too_long(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: toolongifacename
            """))


def test_routing_table_start_out_of_range(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              routing_table_start: 300
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: eth0
            """))


def test_routing_table_overflow_with_uplinks(tmp_path):
    # routing_table_start=252 + 1 uplink → table_end=253 > 252
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              routing_table_start: 252
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: eth0
            """))


def test_ipv6_pd_hint_out_of_range(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: eth0
                  ipv6_pd_hint: 65
            """))


def test_metric_zero(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks:
                - name: isp
                  interface: eth0
                  metric: 0
            """))


def test_no_uplinks(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks:
                - name: lan
                  interface: eth1
              uplinks: []
            """))


def test_no_networks(tmp_path):
    with pytest.raises(SystemExit):
        load(_cfg_file(tmp_path, """
            uplinkmgr:
              networks: []
              uplinks:
                - name: isp
                  interface: eth0
            """))
