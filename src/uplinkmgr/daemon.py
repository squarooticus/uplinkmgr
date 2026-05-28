"""uplinkmgr daemon — main loop and signal handling."""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config, UplinkConfig, load as load_config
from .state import (IPv6PdState, read_ipv4_state, read_ipv6gw_state,
                    read_ipv6pd_state, read_ipv6na_state)
from . import monitor, naming, radvd, routing, state
from .statemachine import LinkState, UplinkState, update as sm_update

log = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/run/uplinkmgr"
PID_FILE_NAME = "uplinkmgr.pid"

_MISSING = object()  # sentinel for "never installed" in macvlan_fwd dict


@dataclass
class _UplinkRouting:
    """Tracks routing elements installed by the daemon for one uplink."""
    ipv4_installed: Optional[str] = None         # installed IPv4 gateway, or None
    ipv6_route_installed: bool = False            # per-uplink IPv6 default route present
    lo_to_uplink_addr: Optional[str] = None      # from-addr in lo_to_uplink rule
    macvlan_internal: set[str] = field(default_factory=set)
    macvlan_fwd: dict[str, Optional[str]] = field(default_factory=dict)  # mv -> prefix or None
    macvlan_prohibit: set[str] = field(default_factory=set)


class Daemon:
    def __init__(self, config_path: str, state_dir: str) -> None:
        self._config_path = config_path
        self._state_dir = state_dir
        self._cfg: Optional[Config] = None
        self._states: dict[str, UplinkState] = {}
        self._installed: dict[str, _UplinkRouting] = {}
        self._ipv4_rules_installed: bool = False
        self._reload_requested = False
        self._reconcile_requested = False
        self._radvd_restart_requested = False
        self._last_radvd_restart: float = 0.0
        self._running = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._setup_signals()
        self._cfg = load_config(self._config_path)
        self._init_states()
        self._write_pid()
        log.info("uplinkmgr started (pid %d)", os.getpid())
        self._setup_ipv4_rules()
        self._reconcile_all()
        try:
            self._loop()
        finally:
            self._cleanup()

    def _loop(self) -> None:
        while self._running:
            if self._reload_requested:
                self._do_reload()

            if self._radvd_restart_requested:
                self._do_radvd_restart()

            if self._reconcile_requested:
                self._do_reconcile()

            start = time.monotonic()
            self._run_cycle()
            elapsed = time.monotonic() - start

            interval = self._cfg.monitor.interval
            if elapsed > interval:
                log.warning("monitoring cycle took %.1fs, exceeds interval %ds",
                            elapsed, interval)
            else:
                self._sleep(interval - elapsed)

    def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early on SIGUSR1, SIGUSR2, or SIGHUP."""
        deadline = time.monotonic() + seconds
        while (self._running and not self._reload_requested
               and not self._reconcile_requested and not self._radvd_restart_requested):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.5))

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _setup_signals(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_term)
        signal.signal(signal.SIGINT, self._handle_term)
        signal.signal(signal.SIGHUP, self._handle_hup)
        signal.signal(signal.SIGUSR1, self._handle_usr1)
        signal.signal(signal.SIGUSR2, self._handle_usr2)

    def _handle_term(self, signum, frame) -> None:
        log.info("received signal %d, shutting down", signum)
        self._running = False

    def _handle_hup(self, signum, frame) -> None:
        log.info("received SIGHUP, will reload config")
        self._reload_requested = True

    def _handle_usr1(self, signum, frame) -> None:
        self._reconcile_requested = True

    def _handle_usr2(self, signum, frame) -> None:
        self._radvd_restart_requested = True

    # ------------------------------------------------------------------
    # Reload
    # ------------------------------------------------------------------

    def _do_reload(self) -> None:
        self._reload_requested = False
        log.info("reloading config from %s", self._config_path)
        try:
            new_cfg = load_config(self._config_path)
        except SystemExit:
            log.error("config reload failed; keeping existing config")
            return
        self._teardown_all()
        self._cfg = new_cfg
        self._init_states()
        self._setup_ipv4_rules()
        self._reconcile_all()
        log.info("config reloaded; all uplink states reset to UP")

    # ------------------------------------------------------------------
    # SIGUSR1 — reconcile routing, rate-limited radvd restart
    # SIGUSR2 — unconditional radvd restart (admin override)
    # ------------------------------------------------------------------

    def _do_radvd_restart(self) -> None:
        self._radvd_restart_requested = False
        log.debug("SIGUSR2: unconditional radvd restart")
        radvd.regenerate_all(
            cfg=self._cfg,
            states=self._states,
            state_dir=self._state_dir,
            action="restart",
        )
        self._last_radvd_restart = time.monotonic()

    def _do_reconcile(self) -> None:
        self._reconcile_requested = False
        self._reconcile_all()

        min_interval = self._cfg.radvd_min_restart_interval
        now_mono = time.monotonic()
        elapsed = now_mono - self._last_radvd_restart
        min_remaining = self._min_gw_remaining()

        if (elapsed >= min_interval
                or (min_remaining is not None and min_remaining <= min_interval)):
            log.debug("SIGUSR1: restarting radvd (elapsed=%.0fs, min_gw_remaining=%s)",
                      elapsed, min_remaining)
            radvd.regenerate_all(
                cfg=self._cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="restart",
            )
            self._last_radvd_restart = now_mono
        else:
            log.debug("SIGUSR1: skipping radvd restart "
                      "(elapsed=%.0fs < interval=%ds, min_gw_remaining=%s)",
                      elapsed, min_interval, min_remaining)
            radvd.regenerate_all(
                cfg=self._cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="write",
            )

    def _min_gw_remaining(self) -> Optional[int]:
        """Return the minimum remaining upstream RA lifetime across all IPv6 uplinks.

        Returns None when all uplinks have infinite lifetime (nd1_lifetime=0) or no state.
        """
        now = int(time.time())
        min_val: Optional[int] = None
        for uplink in self._cfg.uplinks:
            if not uplink.ipv6_pd:
                continue
            gw = state.read_ipv6gw_state(self._state_dir, uplink.name)
            if gw is None or gw.nd1_lifetime == 0:
                continue
            remaining = gw.remaining_lifetime(now)
            if min_val is None or remaining < min_val:
                min_val = remaining
        return min_val

    # ------------------------------------------------------------------
    # Monitoring cycle
    # ------------------------------------------------------------------

    def _run_cycle(self) -> None:
        cfg = self._cfg
        any_ipv6_change = False

        for uplink in cfg.uplinks:
            st = self._states[uplink.name]
            tbl = naming.table_num(cfg.routing_table_start, uplink.index)

            ipv4_ok = monitor.probe_ipv4(uplink.interface, cfg.monitor.v4_hosts)
            log.debug("%s ipv4: %s", uplink.name, "ok" if ipv4_ok else "fail")

            ipv6_ok = True
            ipv6_probe_enabled = False
            if uplink.ipv6_pd:
                if monitor.ipv6_default_route_exists(tbl, uplink.interface):
                    ipv6_probe_enabled = True
                    ipv6_ok = monitor.probe_ipv6(uplink.interface, cfg.monitor.v6_hosts)
                    log.debug("%s ipv6: %s", uplink.name, "ok" if ipv6_ok else "fail")
                else:
                    log.debug("%s ipv6: no default route in table %d, skipping probe",
                              uplink.name, tbl)

            ipv4_changed, ipv6_changed = sm_update(
                state=st,
                ipv4_ok=ipv4_ok,
                ipv6_ok=ipv6_ok,
                ipv6_enabled=ipv6_probe_enabled,
                failure_threshold=cfg.monitor.failure_threshold,
                recovery_threshold=cfg.monitor.recovery_threshold,
            )

            if ipv4_changed:
                log.info("uplink %s ipv4 -> %s", uplink.name, st.ipv4.value)
                self._reconcile_uplink_ipv4(uplink)

            if ipv6_changed:
                any_ipv6_change = True
                log.info("uplink %s ipv6 -> %s", uplink.name, st.ipv6.value)
                self._reconcile_uplink_ipv6(uplink)

        if any_ipv6_change:
            radvd.regenerate_all(
                cfg=cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="sighup",
            )

    # ------------------------------------------------------------------
    # Routing reconcile
    # ------------------------------------------------------------------

    def _setup_ipv4_rules(self) -> None:
        cfg = self._cfg
        routing.add_ipv4_policy_rules(
            suppress_priority=naming.ipv4_suppress_priority(cfg),
            lookup_priority=naming.ipv4_lookup_priority(cfg),
            ipv4_table=naming.ipv4_table_num(cfg.routing_table_start),
        )
        self._ipv4_rules_installed = True

    def _reconcile_all(self) -> None:
        for uplink in self._cfg.uplinks:
            self._reconcile_uplink_ipv4(uplink)
            self._reconcile_uplink_ipv6(uplink)

    def _reconcile_uplink_ipv4(self, uplink: UplinkConfig) -> None:
        """Reconcile IPv4 route in the uplinkmgr table for one uplink."""
        cfg = self._cfg
        installed = self._installed[uplink.name]
        health = self._states[uplink.name]
        ipv4_tbl = naming.ipv4_table_num(cfg.routing_table_start)

        ipv4_st = read_ipv4_state(self._state_dir, uplink.name)
        desired_ipv4_gw = (
            ipv4_st.gateway
            if ipv4_st is not None and health.ipv4 == LinkState.UP
            else None
        )
        if desired_ipv4_gw != installed.ipv4_installed:
            if desired_ipv4_gw is not None:
                routing.replace_ipv4_route(
                    desired_ipv4_gw, uplink.interface, uplink.metric, ipv4_tbl,
                )
            else:
                routing.del_ipv4_route(uplink.interface, ipv4_tbl)
            installed.ipv4_installed = desired_ipv4_gw

    def _reconcile_uplink_ipv6(self, uplink: UplinkConfig) -> None:
        """Reconcile IPv6 route and rules in the per-uplink table for one uplink."""
        if not uplink.ipv6_pd:
            return

        cfg = self._cfg
        now = int(time.time())
        installed = self._installed[uplink.name]
        ipv6_tbl = naming.table_num(cfg.routing_table_start, uplink.index)
        ipv6gw_st = read_ipv6gw_state(self._state_dir, uplink.name)
        ipv6pd_st = read_ipv6pd_state(self._state_dir, uplink.name)
        ipv6na_st = read_ipv6na_state(self._state_dir, uplink.name)

        # IPv6 default route: always replace to refresh expiry
        if ipv6gw_st is not None:
            routing.replace_ipv6_route(
                ipv6gw_st.gateway, uplink.interface, ipv6_tbl,
                ipv6gw_st.nd1_lifetime, ipv6gw_st.remaining_lifetime(now),
            )
            installed.ipv6_route_installed = True
        elif installed.ipv6_route_installed:
            routing.del_ipv6_route(uplink.interface, ipv6_tbl)
            installed.ipv6_route_installed = False

        # lo_to_uplink rule: present iff an uplink address is known AND health is UP
        health = self._states[uplink.name]
        if health.ipv6 != LinkState.UP:
            uplink_addr = None
        elif ipv6na_st is not None:
            uplink_addr = ipv6na_st.address          # managed: DHCPv6 IA_NA
        elif ipv6gw_st is not None and ipv6gw_st.address:
            uplink_addr = ipv6gw_st.address          # unmanaged: SLAAC from RA
        else:
            uplink_addr = None
        if uplink_addr != installed.lo_to_uplink_addr:
            if installed.lo_to_uplink_addr is not None:
                routing.del_ipv6_rule(
                    naming.lo_to_uplink_priority(cfg, uplink.index)
                )
            if uplink_addr is not None:
                routing.add_lo_to_uplink_rule(
                    uplink_addr, ipv6_tbl,
                    naming.lo_to_uplink_priority(cfg, uplink.index),
                )
            installed.lo_to_uplink_addr = uplink_addr

        # Per-macvlan rules: driven by ipv6pd_st presence
        if ipv6pd_st is not None:
            self._reconcile_macvlan_rules(uplink, ipv6pd_st, ipv6_tbl, installed)
        else:
            self._teardown_macvlan_rules(uplink, installed)

    def _reconcile_macvlan_rules(self, uplink: UplinkConfig, ipv6pd_st: IPv6PdState,
                                   ipv6_tbl: int, installed: _UplinkRouting) -> None:
        cfg = self._cfg
        # fwd_to_uplink uses the full delegated prefix as the from-constraint
        delegated = (f"{ipv6pd_st.delegated_prefix}/{ipv6pd_st.delegated_length}"
                     if cfg.reject_incompatible_src else None)

        for net_idx, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            int_prio = naming.internal_traffic_priority(cfg, uplink.index, net_idx)
            fwd_prio = naming.fwd_to_uplink_priority(cfg, uplink.index, net_idx)

            if mv not in installed.macvlan_internal:
                routing.add_internal_traffic_rule(mv, int_prio)
                installed.macvlan_internal.add(mv)

            # fwd_to_uplink: reinstall only when prefix constraint changes
            current_fwd = installed.macvlan_fwd.get(mv, _MISSING)
            if current_fwd is _MISSING:
                routing.add_fwd_to_uplink_rule(mv, ipv6_tbl, fwd_prio, delegated)
                installed.macvlan_fwd[mv] = delegated
            elif current_fwd != delegated:
                routing.del_ipv6_rule(fwd_prio)
                routing.add_fwd_to_uplink_rule(mv, ipv6_tbl, fwd_prio, delegated)
                installed.macvlan_fwd[mv] = delegated

            if cfg.reject_incompatible_src:
                prohibit_prio = naming.prohibit_wrong_src_priority(
                    cfg, uplink.index, net_idx
                )
                if mv not in installed.macvlan_prohibit:
                    routing.add_prohibit_wrong_src_rule(mv, prohibit_prio)
                    installed.macvlan_prohibit.add(mv)

    def _teardown_macvlan_rules(self, uplink: UplinkConfig,
                                 installed: _UplinkRouting) -> None:
        cfg = self._cfg
        for net_idx, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            if mv in installed.macvlan_internal:
                routing.del_ipv6_rule(
                    naming.internal_traffic_priority(cfg, uplink.index, net_idx)
                )
                installed.macvlan_internal.discard(mv)
            if mv in installed.macvlan_fwd:
                routing.del_ipv6_rule(
                    naming.fwd_to_uplink_priority(cfg, uplink.index, net_idx)
                )
                del installed.macvlan_fwd[mv]
            if mv in installed.macvlan_prohibit:
                routing.del_ipv6_rule(
                    naming.prohibit_wrong_src_priority(cfg, uplink.index, net_idx)
                )
                installed.macvlan_prohibit.discard(mv)

    def _teardown_all(self) -> None:
        """Remove all routing elements installed by this daemon."""
        cfg = self._cfg
        ipv4_tbl = naming.ipv4_table_num(cfg.routing_table_start)

        for uplink in cfg.uplinks:
            installed = self._installed[uplink.name]

            if installed.ipv4_installed is not None:
                routing.del_ipv4_route(uplink.interface, ipv4_tbl)
                installed.ipv4_installed = None

            if not uplink.ipv6_pd:
                continue

            ipv6_tbl = naming.table_num(cfg.routing_table_start, uplink.index)

            if installed.ipv6_route_installed:
                routing.del_ipv6_route(uplink.interface, ipv6_tbl)
                installed.ipv6_route_installed = False

            if installed.lo_to_uplink_addr is not None:
                routing.del_ipv6_rule(
                    naming.lo_to_uplink_priority(cfg, uplink.index)
                )
                installed.lo_to_uplink_addr = None

            self._teardown_macvlan_rules(uplink, installed)

        if self._ipv4_rules_installed:
            routing.del_ipv4_policy_rules(
                suppress_priority=naming.ipv4_suppress_priority(cfg),
                lookup_priority=naming.ipv4_lookup_priority(cfg),
            )
            self._ipv4_rules_installed = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_states(self) -> None:
        self._states = {
            u.name: UplinkState(name=u.name)
            for u in self._cfg.uplinks
        }
        self._installed = {
            u.name: _UplinkRouting()
            for u in self._cfg.uplinks
        }

    def _write_pid(self) -> None:
        pid_path = Path(self._state_dir) / PID_FILE_NAME
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()) + "\n")

    def _cleanup(self) -> None:
        self._teardown_all()
        pid_path = Path(self._state_dir) / PID_FILE_NAME
        try:
            pid_path.unlink()
        except OSError:
            pass
        log.info("uplinkmgr stopped")
