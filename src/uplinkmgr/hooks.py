"""State-change event hooks: discovery, shadow-resolution, and execution.

Distinct from "the hook" (hooks/50-uplinkmgr, dhcpcd's exit hook) -- these are
administrator/system scripts run by the daemon itself on events only it can
observe (reachability transitions, primary-uplink changes, reload,
lifecycle). Modeled on NetworkManager's dispatcher.d: individually exec'd (no
shell), argv=(event, uplink), a fixed minimal environment, and the same
regular-file / executable / non-group-or-other-writable / non-setuid
eligibility requirements.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import procrun
from .config import DEFAULT_HOOK_TIMEOUT

log = logging.getLogger(__name__)

HOOKS_SYSTEM_DIR = "/usr/libexec/uplinkmgr/hooks"
HOOKS_USER_DIR = "/etc/uplinkmgr/hooks"

# Fixed PATH for hook scripts, independent of the daemon's own environment --
# same rationale as ifupdown's if-up.d/if-down.d.
_HOOK_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_STEM_PREFIX_RE = re.compile(r'^[0-9]+-')
_BACKUP_SUFFIXES = (".bak", ".dpkg-old", ".dpkg-new", ".dpkg-dist", ".dpkg-tmp",
                    ".rpmsave", ".rpmnew", ".rpmorig", ".swp", "~")


def _stem(filename: str) -> str:
    """Filename with a leading run of digits + hyphen stripped once."""
    return _STEM_PREFIX_RE.sub("", filename, count=1)


def _is_candidate(filename: str) -> bool:
    """Directory-entry filter applied before shadow-stem resolution --
    excludes hidden files and known editor/packaging backup patterns, which
    are never considered hooks at all (not even for shadowing purposes).
    Distinct from _is_eligible(), which gates whether a *surviving* entry is
    actually safe to execute."""
    if filename.startswith("."):
        return False
    if filename.endswith(_BACKUP_SUFFIXES):
        return False
    return True


def _list_dir(directory: str) -> list[str]:
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    return [name for name in entries if _is_candidate(name)]


def resolve_hook_scripts(system_dir: str, user_dir: str) -> list[Path]:
    """Return the ordered list of hook script paths to attempt for one event.

    Aggregates system_dir and user_dir into a single list sorted by filename
    (lexical, C-locale order). A user_dir entry shadows any system_dir entry
    sharing the same stem -- the system entry is dropped unconditionally,
    even if the shadowing user entry itself later fails _is_eligible() (fail
    closed: this also lets an admin disable a system hook outright with a
    non-executable same-stem placeholder in user_dir).
    """
    user_entries = _list_dir(user_dir)
    system_entries = _list_dir(system_dir)

    user_stems = {_stem(name) for name in user_entries}
    system_entries = [name for name in system_entries if _stem(name) not in user_stems]

    survivors = [(user_dir, name) for name in user_entries]
    survivors += [(system_dir, name) for name in system_entries]
    survivors.sort(key=lambda pair: pair[1])
    return [Path(d) / name for d, name in survivors]


def _is_eligible(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if not (st.st_mode & 0o111):
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
        return False
    return True


def _build_env(event: str, uplink: str, env_extra: dict,
                state_dir: str, config_path: str) -> dict:
    env = {
        "PATH": _HOOK_PATH,
        "UPLINKMGR_EVENT": event,
        "UPLINKMGR_TIMESTAMP": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "UPLINKMGR_STATE_DIR": state_dir,
        "UPLINKMGR_CONFIG_PATH": config_path,
    }
    if uplink:
        env["UPLINKMGR_UPLINK"] = uplink
    for key, value in env_extra.items():
        if value is None or value == "":
            continue
        env[f"UPLINKMGR_{key.upper()}"] = str(value)
    return env


def run_hooks_sync(event: str, uplink: str, env_extra: dict, *, timeout: int,
                    system_dir: str, user_dir: str,
                    state_dir: str, config_path: str) -> None:
    """Run every eligible hook script for one event, in order, blocking until
    all have finished (or been killed for exceeding timeout). Never raises --
    logs and continues past any single script's failure."""
    env = _build_env(event, uplink, env_extra, state_dir, config_path)
    for path in resolve_hook_scripts(system_dir, user_dir):
        if not _is_eligible(path):
            log.debug("skipping ineligible hook %s", path)
            continue
        _run_one(path, event, uplink, env, timeout)


def _run_one(path: Path, event: str, uplink: str, env: dict, timeout: int) -> None:
    cmd = [str(path), event, uplink]
    procrun.log_command(log, cmd, env)
    try:
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout can kill descendants too
        )
    except OSError as e:
        log.warning("hook %s failed to execute: %s", path, e)
        return

    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("hook %s timed out after %ds; killing", path, timeout)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return

    if proc.returncode != 0:
        log.warning("hook %s exited %d: %s", path, proc.returncode,
                    stderr.decode(errors="replace").strip())


class HookRunner:
    """Serializes hook execution onto one dedicated background thread, so a
    slow or hung script never delays the daemon's probe/reconcile loop.
    Queued events run one at a time, in FIFO order."""

    def __init__(self, state_dir: str, config_path: str,
                 system_dir: str = HOOKS_SYSTEM_DIR, user_dir: str = HOOKS_USER_DIR,
                 timeout: int = DEFAULT_HOOK_TIMEOUT) -> None:
        self.state_dir = state_dir
        self.config_path = config_path
        self.system_dir = system_dir
        self.user_dir = user_dir
        self.timeout = timeout
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def fire(self, event: str, uplink: str = "", **env_extra) -> None:
        """Enqueue an event for the worker thread and return immediately."""
        self._queue.put((event, uplink, env_extra))

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            event, uplink, env_extra = item
            try:
                run_hooks_sync(
                    event, uplink, env_extra, timeout=self.timeout,
                    system_dir=self.system_dir, user_dir=self.user_dir,
                    state_dir=self.state_dir, config_path=self.config_path,
                )
            except Exception:
                log.exception("unhandled error running hooks for event %s", event)

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """Stop accepting new work after draining what's already queued."""
        self._queue.put(None)
        self._thread.join(timeout=timeout)
