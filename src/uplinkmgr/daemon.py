"""uplinkmgr daemon — main loop and signal handling."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config, load as load_config
from . import monitor, naming, radvd, routing, state
from .statemachine import LinkState, UplinkState, update as sm_update

log = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/run/uplinkmgr"
PID_FILE_NAME = "uplinkmgr.pid"


class Daemon:
    def __init__(self, config_path: str, state_dir: str) -> None:
        self._config_path = config_path
        self._state_dir = state_dir
        self._cfg: Optional[Config] = None
        self._states: dict[str, UplinkState] = {}
        self._reload_requested = False
        self._sigusr1_received = False
        self._sigusr2_received = False
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

        try:
            self._loop()
        finally:
            self._cleanup()

    def _loop(self) -> None:
        while self._running:
            if self._reload_requested:
                self._do_reload()

            if self._sigusr2_received:
                self._do_sigusr2()

            if self._sigusr1_received:
                self._do_sigusr1()

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
               and not self._sigusr1_received and not self._sigusr2_received):
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
        self._sigusr1_received = True

    def _handle_usr2(self, signum, frame) -> None:
        self._sigusr2_received = True

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
        self._cfg = new_cfg
        self._init_states()
        log.info("config reloaded; all uplink states reset to UP")

    # ------------------------------------------------------------------
    # SIGUSR1 — new RA or PD state from hook (rate-limited radvd restart)
    # SIGUSR2 — unconditional radvd restart (admin override)
    # ------------------------------------------------------------------

    def _do_sigusr2(self) -> None:
        self._sigusr2_received = False
        log.debug("SIGUSR2: unconditional radvd restart")
        radvd.regenerate_all(
            cfg=self._cfg,
            states=self._states,
            state_dir=self._state_dir,
            action="restart",
        )
        self._last_radvd_restart = time.monotonic()

    def _do_sigusr1(self) -> None:
        self._sigusr1_received = False
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
                continue  # absent or infinite
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

            # IPv4 probe
            ipv4_ok = monitor.probe_ipv4(uplink.interface, cfg.monitor.v4_hosts)
            log.debug("%s ipv4: %s", uplink.name, "ok" if ipv4_ok else "fail")

            # IPv6 probe (conditional on preconditions)
            ipv6_ok = True  # default: don't change state if not probing
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
                self._apply_ipv4_change(uplink, st)

            if ipv6_changed:
                any_ipv6_change = True
                log.info("uplink %s ipv6 -> %s", uplink.name, st.ipv6.value)

        if any_ipv6_change:
            radvd.regenerate_all(
                cfg=cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="sighup",  # preference change only → SIGHUP
            )

    def _apply_ipv4_change(self, uplink, st: UplinkState) -> None:
        log.info("uplink %s ipv4 -> %s", uplink.name, st.ipv4.value)
        ipv4_st = state.read_ipv4_state(self._state_dir, uplink.name)

        if st.ipv4 == LinkState.DOWN:
            if ipv4_st:
                routing.del_ipv4_default(
                    gateway=ipv4_st.gateway,
                    wan_iface=uplink.interface,
                    metric=uplink.metric,
                )
            else:
                log.warning("%s: DOWN but no ipv4 state file; route may already be absent",
                            uplink.name)
        else:  # UP (recovery)
            if ipv4_st:
                routing.add_ipv4_default(
                    gateway=ipv4_st.gateway,
                    wan_iface=uplink.interface,
                    metric=uplink.metric,
                )
            else:
                log.warning("%s: recovered but no ipv4 state file; "
                            "dhcpcd will restore route when lease is obtained",
                            uplink.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_states(self) -> None:
        self._states = {
            u.name: UplinkState(name=u.name)
            for u in self._cfg.uplinks
        }

    def _write_pid(self) -> None:
        pid_path = Path(self._state_dir) / PID_FILE_NAME
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()) + "\n")

    def _cleanup(self) -> None:
        pid_path = Path(self._state_dir) / PID_FILE_NAME
        try:
            pid_path.unlink()
        except OSError:
            pass
        log.info("uplinkmgr stopped")
