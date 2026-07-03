"""DEBUG-level logging of external commands, in copy-pasteable bash form."""

from __future__ import annotations

import logging
import shlex
from typing import Mapping, Optional, Sequence


def format_command(cmd: Sequence[str], env: Optional[Mapping[str, str]] = None) -> str:
    """Render cmd (plus any env overrides) as a single bash-pasteable line,
    quoting only where the shell requires it."""
    parts = [f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items()]
    parts.append(shlex.join(str(a) for a in cmd))
    return " ".join(parts)


def log_command(log: logging.Logger, cmd: Sequence[str],
                 env: Optional[Mapping[str, str]] = None) -> None:
    if log.isEnabledFor(logging.DEBUG):
        log.debug("+ %s", format_command(cmd, env))
