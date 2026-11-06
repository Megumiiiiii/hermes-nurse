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

from . import backends as _backends
from . import schemas, tools

logger = logging.getLogger(__name__)

_SNAPSHOT_NAME = "last-snapshot.json"

# Verbs that change state. All of them — including `stop` and `reload` — go
# through Hermes' human-approval gate, because the model's own `confirm` flag is
# not a human: under the gateway or a cron run nothing sits in front of it.
_STATE_CHANGING_VERBS = {"restart", "start", "stop", "reload", "reset", "reset-failed"}


def _state_dir() -> str:
    env = os.environ.get("HERMES_PLUGIN_DATA_DIR")
    if env:
        base = env
    else:
        base = os.path.join(_backends._hermes_home(), "plugin-data", "nurse")
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


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_kwargs: Any) -> Any:
    """Escalate every state-changing service_repair call to the human gate.

    ``confirm`` is a flag the model passes to itself, so on the gateway or in a
    cron run it is not a second party at all: the agent could stop a gateway, a
    tunnel or any user unit with nothing in front of it. Returning ``approve``
    hands the call to Hermes' own approval prompt — the same gate a `pm2 stop`
    typed into the terminal tool hits. A plan-only call (``confirm`` false or
    absent) changes nothing and is left alone, so diagnosis stays unattended.

    The cooldown ledger and the crash-loop refusal still run afterwards: this
    hook adds a human, it does not replace the other guards.
    """
    if tool_name != "service_repair":
        return None
    a = args if isinstance(args, dict) else {}
    verb = str(a.get("action") or "restart")
    if verb not in _STATE_CHANGING_VERBS:
        return None
    if not bool(a.get("confirm")):
        return None
    name = a.get("service")
    target = name if isinstance(name, str) and name else "service"
    return {"action": "approve", "message": f"{verb} {target}"}


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
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
