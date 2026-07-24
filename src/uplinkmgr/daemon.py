"""uplinkmgr daemon — main loop and signal handling."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config, UplinkConfig, load as load_config
from .state import (IPv6PdState, read_ipv4_state, read_ipv6ra_state,
                    read_ipv6pd_state, read_ipv6na_state, write_atomic)
from . import hooks, monitor, naming, priority, procrun, radvd, routing, state
from .statemachine import LinkState, UplinkState, update as sm_update

log = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/run/uplinkmgr"
PID_FILE_NAME = "uplinkmgr.pid"
_DHCPCD_PID_FILE = "/run/dhcpcd/pid"  # dhcpcd(8) FILES: PID of the manager-mode instance


@dataclass
class _UplinkRouting:
    """Tracks routing elements installed by the daemon for one uplink."""
    ipv4_installed: Optional[str] = None              # installed IPv4 gateway, or None
    ipv4_lo_to_uplink_addr: Optional[str] = None      # from-addr in IPv4 lo_to_uplink rule
    ipv6_route_installed: bool = False                 # per-uplink IPv6 default route present
    lo_to_uplink_prefix: Optional[str] = None         # from-prefix in IPv6 lo_to_uplink rule
    macvlan_fwd: dict[str, Optional[str]] = field(default_factory=dict)  # mv -> prefix or None
    macvlan_prohibit: set[str] = field(default_factory=set)


class Daemon:
    def __init__(self, config_path: str, state_dir: str,
                 hooks_system_dir: str = hooks.HOOKS_SYSTEM_DIR,
                 hooks_user_dir: str = hooks.HOOKS_USER_DIR) -> None:
        self._config_path = config_path
        self._state_dir = state_dir
        self._cfg: Optional[Config] = None
        self._states: dict[str, UplinkState] = {}
        self._installed: dict[str, _UplinkRouting] = {}
        self._ipv4_rules_installed: bool = False
        self._ipv6_rule_installed: bool = False
        self._reload_requested = False
        self._reconcile_requested = False
        self._radvd_sighup_requested = False
        self._last_radvd_sighup: float = 0.0
        self._last_probe: float = float("-inf")  # monotonic; -inf => probe immediately
        self._running = True
        self._executor: Optional[ThreadPoolExecutor] = None
        self._hooks = hooks.HookRunner(state_dir=state_dir, config_path=config_path,
                                        system_dir=hooks_system_dir, user_dir=hooks_user_dir)
        self._primary_ipv4: Optional[str] = None
        self._primary_ipv6: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._setup_signals()
        self._cfg = load_config(self._config_path)
        self._hooks.timeout = self._cfg.hook_timeout
        self._init_states()
        self._executor = ThreadPoolExecutor(max_workers=len(self._cfg.uplinks))
        self._write_pid()
        self._write_hook_debug_flag()
        log.info("uplinkmgr started (pid %d)", os.getpid())
        self._reconfigure_dhcpcd()
        self._setup_ipv4_rules()
        self._setup_ipv6_rules()
        self._setup_ipv6_macvlan_rules()
        self._reconcile_all()
        self._update_primary_uplinks()
        self._hooks.fire("daemon-start")
        try:
            self._loop()
        finally:
            self._cleanup()

    def _reconfigure_dhcpcd(self) -> None:
        """Ask an already-running dhcpcd to replay hooks for every interface's
        current state, so uplinkmgr catches up on anything it missed while it
        wasn't running to receive the original hook-triggered SIGUSR1.

        Skipped if dhcpcd isn't running yet: there's nothing to catch up on,
        and dhcpcd's CLI may fall back to starting a fresh master process
        (using the system default config, not ours) when no live master is
        found to reconfigure -- see SPEC.md §17."""
        if not self._dhcpcd_is_running():
            log.debug("dhcpcd not running yet; skipping reconfigure")
            return
        cmd = ["dhcpcd", "-g"]
        procrun.log_command(log, cmd)
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            log.warning("dhcpcd -g (reconfigure) timed out")
            return
        if result.returncode != 0:
            log.warning("dhcpcd -g (reconfigure) failed: %s",
                        result.stderr.decode().strip())

    @staticmethod
    def _dhcpcd_is_running() -> bool:
        try:
            pid = int(Path(_DHCPCD_PID_FILE).read_text().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # pid no longer exists -- stale pid file
        except PermissionError:
            return True  # pid exists, we just can't signal it (e.g. owned by root)
        except OSError:
            return False
        return True

    def _loop(self) -> None:
        while self._running:
            if self._reload_requested:
                self._do_reload()

            if self._radvd_sighup_requested:
                self._do_radvd_sighup()

            if self._reconcile_requested:
                self._do_reconcile()

            # Probe timing is independent of signal wake-ups: a signal storm
            # (e.g. an ISP sending RAs every second, each one a hook SIGUSR1)
            # must not drive probing faster than monitor.interval.
            interval = self._cfg.monitor.interval
            now = time.monotonic()
            if now - self._last_probe >= interval:
                self._last_probe = now
                self._run_cycle()
                elapsed = time.monotonic() - now
                if elapsed > interval:
                    log.warning("monitoring cycle took %.1fs, exceeds interval %ds",
                                elapsed, interval)

            # Sleep until a signal or the next cycle start
            self._sleep(self._last_probe + interval - time.monotonic())

    def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early on SIGUSR1, SIGUSR2, or SIGHUP."""
        deadline = time.monotonic() + seconds
        while (self._running and not self._reload_requested
               and not self._reconcile_requested and not self._radvd_sighup_requested):
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
        self._radvd_sighup_requested = True

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
        self._hooks.timeout = new_cfg.hook_timeout
        self._init_states()
        self._setup_ipv4_rules()
        self._setup_ipv6_rules()
        self._setup_ipv6_macvlan_rules()
        self._reconcile_all()
        self._update_primary_uplinks()
        # States were just reset to optimistic UP; re-validate promptly.
        self._last_probe = float("-inf")
        log.info("config reloaded; all uplink states reset to UP")
        self._hooks.fire("reload")

    # ------------------------------------------------------------------
    # SIGUSR1 — reconcile routing, rate-limited radvd SIGHUP
    # SIGUSR2 — unconditional radvd SIGHUP (admin override)
    # ------------------------------------------------------------------

    def _do_radvd_sighup(self) -> None:
        self._radvd_sighup_requested = False
        log.debug("SIGUSR2: unconditional radvd sighup")
        radvd.regenerate_all(
            cfg=self._cfg,
            states=self._states,
            state_dir=self._state_dir,
            action="sighup",
        )
        self._last_radvd_sighup = time.monotonic()

    def _do_reconcile(self) -> None:
        self._reconcile_requested = False
        self._reconcile_all()

        min_interval = self._cfg.radvd_min_restart_interval
        now_mono = time.monotonic()
        elapsed = now_mono - self._last_radvd_sighup
        min_remaining = self._min_gw_remaining()

        if (elapsed >= min_interval
                or (min_remaining is not None and min_remaining <= min_interval)):
            log.debug("SIGUSR1: sighuping radvd (elapsed=%.0fs, min_gw_remaining=%s)",
                      elapsed, min_remaining)
            radvd.regenerate_all(
                cfg=self._cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="sighup",
            )
            self._last_radvd_sighup = now_mono
        else:
            log.debug("SIGUSR1: skipping radvd sighup "
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

        Returns None when all uplinks have infinite lifetime (lifetime=0) or no state.
        """
        now = int(time.time())
        min_val: Optional[int] = None
        for uplink in self._cfg.uplinks:
            if not uplink.ipv6_pd and not uplink.ia_na:
                continue
            ra_st = state.read_ipv6ra_state(self._state_dir, uplink.name)
            if ra_st is None or ra_st.lifetime == 0:
                continue
            remaining = ra_st.remaining_lifetime(now)
            if min_val is None or remaining < min_val:
                min_val = remaining
        return min_val

    # ------------------------------------------------------------------
    # Monitoring cycle
    # ------------------------------------------------------------------

    def _probe_uplink(self, uplink: UplinkConfig) -> tuple:
        cfg = self._cfg
        count = cfg.monitor.ping_count

        ipv4_ok = monitor.probe_ipv4(uplink.interface, cfg.monitor.v4_hosts, count)

        ipv6_probe_enabled = uplink.ipv6_pd or uplink.ia_na
        ipv6_ok = True
        if ipv6_probe_enabled:
            # Only the IA_NA address is a single, stable address we can bind
            # to; SLAAC (RA prefix, possibly with privacy addresses) has no
            # one fixed address to hand to ping, so fall back to -I <iface> alone.
            na_st = state.read_ipv6na_state(self._state_dir, uplink.name)
            ipv6_addr = na_st.address if na_st is not None else None
            ipv6_ok = monitor.probe_ipv6(uplink.interface, cfg.monitor.v6_hosts, count, ipv6_addr)

        return uplink.name, ipv4_ok, ipv6_ok, ipv6_probe_enabled

    def _run_cycle(self) -> None:
        cfg = self._cfg

        futures = {
            self._executor.submit(self._probe_uplink, uplink): uplink
            for uplink in cfg.uplinks
        }
        probe_results: dict[str, tuple] = {}
        for f in as_completed(futures):
            name, ipv4_ok, ipv6_ok, enabled = f.result()
            log.debug("%s ipv4: %s", name, "ok" if ipv4_ok else "fail")
            if enabled:
                log.debug("%s ipv6: %s", name, "ok" if ipv6_ok else "fail")
            probe_results[name] = (ipv4_ok, ipv6_ok, enabled)

        any_ipv6_change = False
        for uplink in cfg.uplinks:
            st = self._states[uplink.name]
            ipv4_ok, ipv6_ok, ipv6_probe_enabled = probe_results[uplink.name]

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
                self._fire_wan_event(uplink, "ipv4", st.ipv4)

            if ipv6_changed:
                any_ipv6_change = True
                log.info("uplink %s ipv6 -> %s", uplink.name, st.ipv6.value)
                self._reconcile_uplink_ipv6(uplink)
                self._fire_wan_event(uplink, "ipv6", st.ipv6)

        if any_ipv6_change:
            radvd.regenerate_all(
                cfg=cfg,
                states=self._states,
                state_dir=self._state_dir,
                action="sighup",
            )

        self._update_primary_uplinks()

    def _fire_wan_event(self, uplink: UplinkConfig, family: str, new_state: LinkState) -> None:
        event = "wan-up" if new_state == LinkState.UP else "wan-down"
        gateway = address = prefix = prefix_length = None
        if family == "ipv4":
            ipv4_st = read_ipv4_state(self._state_dir, uplink.name)
            if ipv4_st is not None:
                gateway, address = ipv4_st.gateway, ipv4_st.address
        else:
            ra_st = read_ipv6ra_state(self._state_dir, uplink.name)
            na_st = read_ipv6na_state(self._state_dir, uplink.name)
            if ra_st is not None:
                gateway = ra_st.gateway
                prefix, prefix_length = (ra_st.prefix or None), (ra_st.plen or None)
            if na_st is not None:
                address = na_st.address
        self._hooks.fire(
            event, uplink=uplink.name,
            interface=uplink.interface,
            family=family,
            uplink_index=uplink.index,
            metric=uplink.metric,
            gateway=gateway,
            address=address,
            prefix=prefix,
            prefix_length=prefix_length,
        )

    def _update_primary_uplinks(self) -> None:
        cfg = self._cfg
        up_ipv4 = [u for u in cfg.uplinks if self._states[u.name].ipv4 == LinkState.UP]
        new_ipv4 = min(up_ipv4, key=lambda u: u.metric) if up_ipv4 else None
        new_ipv4_name = new_ipv4.name if new_ipv4 is not None else None
        if new_ipv4_name != self._primary_ipv4:
            self._fire_primary_change("ipv4", self._primary_ipv4, new_ipv4_name,
                                       new_ipv4.interface if new_ipv4 is not None else None)
            self._primary_ipv4 = new_ipv4_name

        new_ipv6 = radvd.primary_ipv6_uplink(cfg, self._states)
        new_ipv6_name = new_ipv6.name if new_ipv6 is not None else None
        if new_ipv6_name != self._primary_ipv6:
            self._fire_primary_change("ipv6", self._primary_ipv6, new_ipv6_name,
                                       new_ipv6.interface if new_ipv6 is not None else None)
            self._primary_ipv6 = new_ipv6_name

    def _fire_primary_change(self, family: str, old: Optional[str], new: Optional[str],
                              interface: Optional[str]) -> None:
        log.info("primary %s uplink: %s -> %s", family, old or "(none)", new or "(none)")
        self._hooks.fire(
            "primary-change", uplink=new or "",
            family=family, old_primary=old, new_primary=new, interface=interface,
        )

    # ------------------------------------------------------------------
    # Routing reconcile
    # ------------------------------------------------------------------

    def _setup_ipv4_rules(self) -> None:
        cfg = self._cfg
        routing.add_ipv4_policy_rules(
            internal_traffic_priority=priority.ipv4_internal_traffic_priority(cfg),
            fwd_to_wan_priority=priority.ipv4_fwd_to_wan_priority(cfg),
            ipv4_table=naming.ipv4_table_num(cfg.routing_table_start),
        )
        self._ipv4_rules_installed = True

    def _setup_ipv6_rules(self) -> None:
        routing.add_ipv6_policy_rule(
            internal_traffic_priority=priority.ipv6_internal_traffic_priority(self._cfg),
        )
        self._ipv6_rule_installed = True

    def _setup_ipv6_macvlan_rules(self) -> None:
        """Install the per-macvlan fwd_to_uplink rule for every (uplink,
        network) pair once, unconditionally, at startup. Never torn down
        while the daemon runs -- only _reconcile_macvlan_rules() narrows it
        further as PD state becomes known; full removal is _teardown_all()'s
        job (daemon shutdown or config reload)."""
        cfg = self._cfg
        for uplink in cfg.uplinks:
            if not uplink.ipv6_pd:
                continue
            installed = self._installed[uplink.name]
            tbl = naming.ipv6_table_num(cfg.routing_table_start, uplink.index)
            for net_idx, net in enumerate(cfg.networks):
                mv = naming.macvlan_name(net.interface, uplink.index)
                fwd_prio = priority.ipv6_fwd_to_uplink_priority(cfg, uplink.index, net_idx)
                routing.add_ipv6_fwd_to_uplink_rule(mv, tbl, fwd_prio, None)
                installed.macvlan_fwd[mv] = None

    def _reconcile_all(self) -> None:
        for uplink in self._cfg.uplinks:
            self._reconcile_uplink_ipv4(uplink)
            self._reconcile_uplink_ipv6(uplink)

    def _reconcile_uplink_ipv4(self, uplink: UplinkConfig) -> None:
        """Reconcile IPv4 routes and rules for one uplink."""
        cfg = self._cfg
        installed = self._installed[uplink.name]
        health = self._states[uplink.name]
        shared_tbl = naming.ipv4_table_num(cfg.routing_table_start)
        per_uplink_tbl = naming.ipv6_table_num(cfg.routing_table_start, uplink.index)

        ipv4_st = read_ipv4_state(self._state_dir, uplink.name)
        uplink_gw = (
            ipv4_st.gateway
            if ipv4_st is not None and health.ipv4 == LinkState.UP
            else None
        )

        # Shared + per-uplink default routes: always in sync, independent of address
        if uplink_gw != installed.ipv4_installed:
            if uplink_gw is not None:
                routing.replace_ipv4_route(uplink_gw, uplink.interface, uplink.metric, shared_tbl)
                routing.replace_ipv4_route(uplink_gw, uplink.interface, 0, per_uplink_tbl)
            else:
                routing.del_ipv4_route(uplink.interface, shared_tbl)
                routing.del_ipv4_route(uplink.interface, per_uplink_tbl)
            installed.ipv4_installed = uplink_gw

        # lo_to_uplink rule: keyed on address, not gateway
        uplink_addr = (
            ipv4_st.address
            if ipv4_st is not None and ipv4_st.address and health.ipv4 == LinkState.UP
            else None
        )
        if uplink_addr != installed.ipv4_lo_to_uplink_addr:
            if installed.ipv4_lo_to_uplink_addr is not None:
                routing.del_ipv4_rule(
                    priority.ipv4_lo_to_uplink_priority(cfg, uplink.index)
                )
            if uplink_addr is not None:
                routing.add_ipv4_lo_to_uplink_rule(
                    uplink_addr, per_uplink_tbl,
                    priority.ipv4_lo_to_uplink_priority(cfg, uplink.index),
                )
            installed.ipv4_lo_to_uplink_addr = uplink_addr

    def _reconcile_uplink_ipv6(self, uplink: UplinkConfig) -> None:
        """Reconcile IPv6 route and rules in the per-uplink table for one uplink."""
        if not uplink.ipv6_pd and not uplink.ia_na:
            return

        cfg = self._cfg
        now = int(time.time())
        installed = self._installed[uplink.name]
        tbl = naming.ipv6_table_num(cfg.routing_table_start, uplink.index)
        ra_st = read_ipv6ra_state(self._state_dir, uplink.name)
        pd_st = read_ipv6pd_state(self._state_dir, uplink.name)
        na_st = read_ipv6na_state(self._state_dir, uplink.name)

        # IPv6 default route: always replace to refresh expiry
        if ra_st is not None:
            routing.replace_ipv6_route(
                ra_st.gateway, uplink.interface, tbl,
                ra_st.lifetime, ra_st.remaining_lifetime(now),
            )
            installed.ipv6_route_installed = True
        elif installed.ipv6_route_installed:
            routing.del_ipv6_route(uplink.interface, tbl)
            installed.ipv6_route_installed = False

        # lo_to_uplink rule: present iff an uplink prefix/address is known AND health is UP
        health = self._states[uplink.name]
        if health.ipv6 != LinkState.UP:
            uplink_prefix = None
        elif na_st is not None:
            uplink_prefix = f"{na_st.address}/128"          # managed: IA_NA specific address
        elif ra_st is not None and ra_st.prefix:
            uplink_prefix = f"{ra_st.prefix}/{ra_st.plen}"  # SLAAC: full RA prefix
        elif ra_st is not None and ra_st.address:
            uplink_prefix = f"{ra_st.address}/128"          # fallback: old state file
        else:
            uplink_prefix = None
        if uplink_prefix != installed.lo_to_uplink_prefix:
            if installed.lo_to_uplink_prefix is not None:
                routing.del_ipv6_rule(
                    priority.ipv6_lo_to_uplink_priority(cfg, uplink.index)
                )
            if uplink_prefix is not None:
                routing.add_ipv6_lo_to_uplink_rule(
                    uplink_prefix, tbl,
                    priority.ipv6_lo_to_uplink_priority(cfg, uplink.index),
                )
            installed.lo_to_uplink_prefix = uplink_prefix

        # Per-macvlan rules: fwd_to_uplink is static (installed once at
        # startup, see _setup_ipv6_macvlan_rules) -- only its from-constraint
        # is updated here as PD state changes. The reject_wrong_pd_src
        # prohibit rule remains driven by PD state presence.
        if pd_st is not None:
            self._reconcile_macvlan_rules(uplink, pd_st, tbl, installed)
        else:
            self._teardown_macvlan_prohibit_rules(uplink, installed)

    def _reconcile_macvlan_rules(self, uplink: UplinkConfig, ipv6pd_st: IPv6PdState,
                                   ipv6_tbl: int, installed: _UplinkRouting) -> None:
        cfg = self._cfg
        # fwd_to_uplink uses the full delegated prefix as the from-constraint
        delegated = (f"{ipv6pd_st.delegated_prefix}/{ipv6pd_st.delegated_length}"
                     if cfg.reject_wrong_pd_src else None)

        for net_idx, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            fwd_prio = priority.ipv6_fwd_to_uplink_priority(cfg, uplink.index, net_idx)

            # fwd_to_uplink always already exists (installed by
            # _setup_ipv6_macvlan_rules); only replace it in place when its
            # from-constraint changes.
            if installed.macvlan_fwd.get(mv) != delegated:
                routing.del_ipv6_rule(fwd_prio)
                routing.add_ipv6_fwd_to_uplink_rule(mv, ipv6_tbl, fwd_prio, delegated)
                installed.macvlan_fwd[mv] = delegated

            if cfg.reject_wrong_pd_src:
                prohibit_prio = priority.ipv6_reject_wrong_pd_src_priority(
                    cfg, uplink.index, net_idx
                )
                if mv not in installed.macvlan_prohibit:
                    routing.add_ipv6_reject_wrong_pd_src_rule(mv, prohibit_prio)
                    installed.macvlan_prohibit.add(mv)

    def _teardown_macvlan_prohibit_rules(self, uplink: UplinkConfig,
                                          installed: _UplinkRouting) -> None:
        """Remove only the reject_wrong_pd_src prohibit rules for one uplink,
        when its PD state disappears. fwd_to_uplink is left untouched -- it's
        static for the daemon's lifetime; see _reconcile_uplink_ipv6."""
        cfg = self._cfg
        for net_idx, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            if mv in installed.macvlan_prohibit:
                routing.del_ipv6_rule(
                    priority.ipv6_reject_wrong_pd_src_priority(cfg, uplink.index, net_idx)
                )
                installed.macvlan_prohibit.discard(mv)

    def _teardown_macvlan_rules(self, uplink: UplinkConfig,
                                 installed: _UplinkRouting) -> None:
        """Remove all macvlan rules (fwd_to_uplink + prohibit) for one
        uplink. Used only by _teardown_all() (daemon shutdown or config
        reload) -- fwd_to_uplink is otherwise static while the daemon runs."""
        cfg = self._cfg
        for net_idx, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            if mv in installed.macvlan_fwd:
                routing.del_ipv6_rule(
                    priority.ipv6_fwd_to_uplink_priority(cfg, uplink.index, net_idx)
                )
                del installed.macvlan_fwd[mv]
            if mv in installed.macvlan_prohibit:
                routing.del_ipv6_rule(
                    priority.ipv6_reject_wrong_pd_src_priority(cfg, uplink.index, net_idx)
                )
                installed.macvlan_prohibit.discard(mv)

    def _teardown_all(self) -> None:
        """Remove all routing elements installed by this daemon."""
        cfg = self._cfg
        ipv4_tbl = naming.ipv4_table_num(cfg.routing_table_start)

        for uplink in cfg.uplinks:
            installed = self._installed[uplink.name]

            per_uplink_tbl = naming.ipv6_table_num(cfg.routing_table_start, uplink.index)
            if installed.ipv4_installed is not None:
                routing.del_ipv4_route(uplink.interface, ipv4_tbl)
                routing.del_ipv4_route(uplink.interface, per_uplink_tbl)
                installed.ipv4_installed = None

            if installed.ipv4_lo_to_uplink_addr is not None:
                routing.del_ipv4_rule(
                    priority.ipv4_lo_to_uplink_priority(cfg, uplink.index)
                )
                installed.ipv4_lo_to_uplink_addr = None

            if not uplink.ipv6_pd and not uplink.ia_na:
                continue

            ipv6_tbl = per_uplink_tbl

            if installed.ipv6_route_installed:
                routing.del_ipv6_route(uplink.interface, ipv6_tbl)
                installed.ipv6_route_installed = False

            if installed.lo_to_uplink_prefix is not None:
                routing.del_ipv6_rule(
                    priority.ipv6_lo_to_uplink_priority(cfg, uplink.index)
                )
                installed.lo_to_uplink_prefix = None

            self._teardown_macvlan_rules(uplink, installed)

        if self._ipv4_rules_installed:
            routing.del_ipv4_policy_rules(
                internal_traffic_priority=priority.ipv4_internal_traffic_priority(cfg),
                fwd_to_wan_priority=priority.ipv4_fwd_to_wan_priority(cfg),
            )
            self._ipv4_rules_installed = False

        if self._ipv6_rule_installed:
            routing.del_ipv6_policy_rule(
                internal_traffic_priority=priority.ipv6_internal_traffic_priority(cfg),
            )
            self._ipv6_rule_installed = False

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

    def _write_hook_debug_flag(self) -> None:
        """Tell the dhcpcd hook (a separate process per lease event) whether
        to log its own actions, mirroring the daemon's own --log-level."""
        path = Path(naming.hook_debug_env_path(self._state_dir))
        if log.isEnabledFor(logging.DEBUG):
            write_atomic(
                str(path),
                f"UPLINKMGR_HOOK_LOG={naming.hook_log_path(self._state_dir)}\n",
            )
        else:
            path.unlink(missing_ok=True)

    def _cleanup(self) -> None:
        self._teardown_all()
        if self._executor is not None:
            self._executor.shutdown(wait=False)

        # Regenerate radvd configs in "all UP" state and SIGHUP so radvd
        # continues advertising correctly after the daemon stops (§13.3).
        if self._cfg is not None:
            all_up_states = {
                u.name: UplinkState(name=u.name, ipv4=LinkState.UP, ipv6=LinkState.UP)
                for u in self._cfg.uplinks
            }
            radvd.regenerate_all(
                cfg=self._cfg,
                states=all_up_states,
                state_dir=self._state_dir,
                action="sighup",
            )
            self._hooks.fire("daemon-stop")
        self._hooks.shutdown(timeout=self._hooks.timeout)

        pid_path = Path(self._state_dir) / PID_FILE_NAME
        try:
            pid_path.unlink()
        except OSError:
            pass
        log.info("uplinkmgr stopped")
