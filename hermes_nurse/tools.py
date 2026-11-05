"""Tool handlers — the code that runs when the model calls a nurse tool.

Every handler returns a JSON string, matching the Hermes tool contract.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import backends, repair, triage


def _args(args: Any = None, kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize how a handler was invoked.

    Hermes dispatches a plugin tool as ``handler(args_dict)`` (``tools/registry``
    calls ``entry.handler(args, **kwargs)``), so a handler that declared its
    parameters individually silently received a dict as its first argument.
    Direct calls and tests pass keywords instead. Accept both.
    """
    merged: Dict[str, Any] = {}
    if isinstance(args, dict):
        merged.update(args)
    elif args is not None:
        # A bare positional non-dict means the caller passed a single value;
        # treat it as the first documented parameter of the handler.
        merged["__positional__"] = args
    if kwargs:
        merged.update(kwargs)
    return merged


def _ok(payload: Dict[str, Any]) -> str:
    payload.setdefault("success", True)
    return json.dumps(payload, indent=2, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra}, indent=2, default=str)


def _fmt_bytes(n: Optional[int]) -> str:
    if not n:
        return "-"
    units = ["B", "K", "M", "G", "T"]
    val = float(n)
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f"{val:.0f}{unit}"
        val /= 1024
    return f"{val:.0f}T"


def _fmt_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def service_list(args: Any = None, **kwargs: Any) -> str:
    """Hermes calls a plugin handler as ``handler(args_dict)`` — the arguments
    arrive as one dict, not as keyword arguments. Accepting them individually
    meant ``only_problems`` received a dict and every call raised."""
    a = _args(args, kwargs)
    only_problems = bool(a.get("only_problems", False))
    supervisor = str(a.get("supervisor") or "all")
    include_system_units = bool(a.get("include_system_units", False))
    try:
        collected = backends.collect(
            user_systemd=not include_system_units,
            include_systemd=supervisor in ("all", "systemd"),
            include_pm2=supervisor in ("all", "pm2"),
        )
    except Exception as exc:  # noqa: BLE001 - never raise into the agent loop
        return _err(f"could not read supervisors: {exc}")

    services = collected["services"]

    if only_problems:
        services = [
            s for s in services
            if s.state in ("stopped", "errored")
            or s.restarts >= repair.CRASH_LOOP_RESTARTS
        ]

    services.sort(key=lambda s: (s.supervisor, s.name))

    rows: List[Dict[str, Any]] = []
    for s in services:
        rows.append({
            "name": s.name,
            "supervisor": s.supervisor,
            "state": s.state,
            "restarts": s.restarts,
            "pid": s.pid,
            "memory": _fmt_bytes(s.memory_bytes),
            "uptime": _fmt_uptime(s.uptime_seconds),
            "exit_code": s.exit_code,
            "crash_loop": s.restarts >= repair.CRASH_LOOP_RESTARTS
            and (s.uptime_seconds or 0) < repair.CRASH_LOOP_UPTIME,
            "script": s.script,
        })

    healthy = sum(1 for s in rows if s["state"] == "running")
    return _ok({
        "count": len(rows),
        "running": healthy,
        "not_running": len(rows) - healthy,
        "services": rows,
        "warnings": collected["errors"],
        "legend": "state: running | stopped | errored | unknown",
    })


def service_triage(args: Any = None, **kwargs: Any) -> str:
    a = _args(args, kwargs)
    service = str(a.get("service") or kwargs.get("service") or a.get("__positional__") or "")
    log_lines = int(a.get("log_lines") or 120)
    try:
        collected = backends.collect()
    except Exception as exc:  # noqa: BLE001
        return _err(f"could not read supervisors: {exc}")

    target = triage.find_service(collected["services"], service)
    if target is None:
        known = sorted({s.name for s in collected["services"]})
        return _err(
            f"no service named {service!r}",
            known_services=known[:60],
            hint="service is not defined, or the name is ambiguous — use service_list",
        )

    try:
        result = triage.diagnose(target)
    except Exception as exc:  # noqa: BLE001
        return _err(f"triage failed for {target.name}: {exc}")

    result["success"] = True
    return json.dumps(result, indent=2, default=str)


def service_repair(args: Any = None, **kwargs: Any) -> str:
    a = _args(args, kwargs)
    service = str(a.get("service") or kwargs.get("service") or a.get("__positional__") or "")
    action = str(a.get("action") or "restart")
    confirm = bool(a.get("confirm", False))
    try:
        collected = backends.collect()
    except Exception as exc:  # noqa: BLE001
        return _err(f"could not read supervisors: {exc}")

    target = triage.find_service(collected["services"], service)
    if target is None:
        return _err(
            f"no service named {service!r}",
            known_services=sorted({s.name for s in collected["services"]})[:60],
        )

    try:
        result = repair.repair(target, verb=action, confirm=confirm)
    except Exception as exc:  # noqa: BLE001
        return _err(f"repair failed for {target.name}: {exc}")

    result["success"] = result.get("ok", False)
    return json.dumps(result, indent=2, default=str)
