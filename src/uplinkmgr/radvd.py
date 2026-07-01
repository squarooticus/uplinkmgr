"""radvd config regeneration and lifecycle management."""

from __future__ import annotations

import ipaddress
import logging
import subprocess
import time
from typing import Optional

from .config import Config, UplinkConfig
from .state import IPv6RaState, IPv6PdState, read_ipv6ra_state, read_ipv6pd_state
from .statemachine import LinkState, UplinkState
from . import naming, generator

log = logging.getLogger(__name__)

# Preference tiers
_PREF_HIGH = "high"
_PREF_MEDIUM = "medium"
_PREF_LOW = "low"

# Fallback lifetime when no RA state is available
_FALLBACK_LIFETIME = generator.INITIAL_DEFAULT_LIFETIME


def regenerate_all(
    cfg: Config,
    states: dict[str, UplinkState],
    state_dir: str,
    action: str,
) -> None:
    """Regenerate radvd configs for all IPv6 uplinks and optionally signal radvd.

    action="restart" → write configs with fresh lifetime values, systemctl restart
    action="sighup"  → write configs, send SIGHUP (preference-change only)
    action="write"   → write configs only, no signal (rate-limited SIGUSR1 path)
    """
    now = int(time.time())

    # Determine preference tiers based on IPv6 state
    ipv6_uplinks = [u for u in cfg.uplinks if u.ipv6_pd]
    up_uplinks = [u for u in ipv6_uplinks if states[u.name].ipv6 == LinkState.UP]

    def preference_for(uplink: UplinkConfig) -> str:
        if states[uplink.name].ipv6 == LinkState.DOWN:
            return _PREF_LOW
        # highest-priority UP uplink gets high; rest get medium
        if up_uplinks and up_uplinks[0].name == uplink.name:
            return _PREF_HIGH
        return _PREF_MEDIUM

    for uplink in ipv6_uplinks:
        pref = preference_for(uplink)
        is_down = states[uplink.name].ipv6 == LinkState.DOWN

        ra_state: Optional[IPv6RaState] = read_ipv6ra_state(state_dir, uplink.name)
        pd_state: Optional[IPv6PdState] = read_ipv6pd_state(state_dir, uplink.name)

        if ra_state is not None:
            default_lifetime = ra_state.remaining_lifetime(now)
        else:
            default_lifetime = _FALLBACK_LIFETIME

        if pd_state is not None:
            valid_lifetime = pd_state.remaining_vltime(now)
            preferred_lifetime = pd_state.remaining_pltime(now)
        else:
            valid_lifetime = generator.INITIAL_VALID_LIFETIME
            preferred_lifetime = generator.INITIAL_PREFERRED_LIFETIME

        # exclusive_preferred_pd: only one UP uplink should ever be visible to
        # clients as a default router, since RFC 6724 rule 5.5 (source/router
        # address selection matching) isn't implemented in most stacks yet --
        # so secondary UP uplinks are fully withdrawn (preferred lifetime and
        # default-router lifetime both zeroed), not just dispreferred.
        is_secondary = (
            cfg.exclusive_preferred_pd
            and not is_down
            and not (up_uplinks and up_uplinks[0].name == uplink.name)
        )
        if is_secondary:
            preferred_lifetime = 0
            default_lifetime = 0

        per_iface_prefixes = _derive_prefixes(cfg, uplink, pd_state)

        conf_text = generator.radvd_conf_from_state(
            cfg=cfg,
            uplink=uplink,
            preference=pref,
            default_lifetime=default_lifetime,
            per_iface_prefixes=per_iface_prefixes,
            valid_lifetime=valid_lifetime,
            preferred_lifetime=preferred_lifetime,
            is_down=is_down,
        )

        conf_path = naming.radvd_conf_path(uplink.name)
        _write_atomic(conf_path, conf_text)

        unit = naming.radvd_unit_name(uplink.name)
        if action == "restart":
            _systemctl("restart", unit)
        elif action == "sighup":
            _sighup(unit)
        # action == "write": configs written, no signal


def _derive_prefixes(
    cfg: Config,
    uplink: UplinkConfig,
    pd_state: Optional[IPv6PdState],
) -> dict[str, str]:
    """Return {macvlan_name: prefix/64} for each network."""
    if pd_state is None:
        return {}
    try:
        delegated = ipaddress.ip_network(
            f"{pd_state.delegated_prefix}/{pd_state.delegated_length}", strict=False
        )
        sla_bits = 64 - pd_state.delegated_length
        if len(cfg.networks) > 2**sla_bits:
            log.warning(f"prefix delegation (/{pd_state.delegated_length}) too small for network count {len(cfg.networks)}")
            return {}
        if sla_bits < 0:
            return {}
        result = {}
        for sla_id, net in enumerate(cfg.networks):
            mv = naming.macvlan_name(net.interface, uplink.index)
            subnet_addr = delegated.network_address + (sla_id << 64)
            prefix = ipaddress.ip_network(f"{subnet_addr}/64")
            result[mv] = str(prefix)
        return result
    except (ValueError, OverflowError) as e:
        log.warning("could not derive prefixes for %s: %s", uplink.name, e)
        return {}


def _write_atomic(path: str, content: str) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(content)
        import os
        os.replace(tmp, path)
    except OSError as e:
        log.error("failed to write %s: %s", path, e)


def _sighup(unit: str) -> None:
    result = subprocess.run(
        ["systemctl", "kill", "--signal=SIGHUP", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        log.warning("SIGHUP %s failed: %s", unit, result.stderr.decode().strip())


def _systemctl(action: str, unit: str) -> None:
    result = subprocess.run(
        ["systemctl", action, unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        log.warning("systemctl %s %s failed: %s",
                    action, unit, result.stderr.decode().strip())
