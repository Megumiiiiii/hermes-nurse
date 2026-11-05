"""hermes-nurse — supervisor-aware service management for Hermes Agent.

Registers three tools (service_list, service_triage, service_repair) and one
session-start hook that surfaces services which died since the last session.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict

from . import schemas, tools

logger = logging.getLogger(__name__)

_SNAPSHOT_NAME = "last-snapshot.json"


def _state_dir() -> str:
    env = os.environ.get("HERMES_PLUGIN_DATA_DIR")
    if env:
        base = env
    else:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        base = os.path.join(home, "plugin-data", "nurse")
    os.makedirs(base, exist_ok=True)
    return base


def _snapshot_path() -> str:
    return os.path.join(_state_dir(), _SNAPSHOT_NAME)


def _read_snapshot() -> Dict[str, Any]:
    try:
        with open(_snapshot_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_snapshot(state: Dict[str, Any]) -> None:
    path = _snapshot_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover - disk full / perms
        logger.debug("nurse: could not persist snapshot: %s", exc)


def _current_states() -> Dict[str, str]:
    """Map '<supervisor>:<name>' -> state, from whatever supervisors exist."""
    from . import backends
    try:
        collected = backends.collect()
    except Exception as exc:  # noqa: BLE001
        logger.debug("nurse: supervisor read failed at session start: %s", exc)
        return {}
    return {f"{s.supervisor}:{s.name}": s.state for s in collected["services"]}


def _on_session_start(**_kwargs: Any) -> None:
    """Report services that went down between sessions, then refresh the snapshot.

    The point is continuity: a crash that happened while Hermes was asleep is
    invisible from inside a fresh session unless something recorded the state
    before and compares it after.
    """
    previous = _read_snapshot().get("states") or {}
    current = _current_states()
    if not current:
        return

    died = [
        key for key, state in previous.items()
        if state == "running" and current.get(key, "gone") != "running"
    ]
    came_back = [
        key for key, state in previous.items()
        if state != "running" and current.get(key) == "running"
    ]

    if died:
        logger.warning(
            "nurse: %d service(s) went down since the last session: %s — "
            "run service_triage on them before restarting",
            len(died), ", ".join(sorted(died)),
        )
    if came_back:
        logger.info("nurse: back up since last session: %s", ", ".join(sorted(came_back)))

    _write_snapshot({"states": current, "updated_at": time.time(), "went_down": died})


def register(ctx) -> None:
    """Wire schemas to handlers and register the session-start hook."""
    ctx.register_tool(
        name="service_list",
        toolset="nurse",
        schema=schemas.SERVICE_LIST,
        handler=tools.service_list,
        description="List pm2 + systemd services with state, restarts, memory and uptime",
        emoji="🩺",
    )
    ctx.register_tool(
        name="service_triage",
        toolset="nurse",
        schema=schemas.SERVICE_TRIAGE,
        handler=tools.service_triage,
        description="Diagnose a down service from its logs, exit code and restart history",
        emoji="🔍",
    )
    ctx.register_tool(
        name="service_repair",
        toolset="nurse",
        schema=schemas.SERVICE_REPAIR,
        handler=tools.service_repair,
        description="Restart/stop/start a service under crash-loop and cooldown guards",
        emoji="🔧",
    )
    ctx.register_hook("on_session_start", _on_session_start)
