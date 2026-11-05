"""Process / service backends for hermes-nurse.

Two supervisors are supported, both read through their *public* interfaces:

* ``pm2``     — via ``pm2 jlist`` JSON (no shelling into the Node internals)
* ``systemd`` — via ``systemctl show -p ...`` (user units by default)

Nothing here mutates state; every write path lives in ``repair.py`` so the
read-only and mutating surfaces stay separable for review.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_TIMEOUT = 8.0

# systemd properties we ask for in one call
SYSTEMD_PROPS = (
    "Id", "ActiveState", "SubState", "NRestarts", "MainPID", "MemoryCurrent",
    "ExecMainStartTimestamp", "Result", "UnitFileState", "LoadState",
    "ExecMainStatus", "ExecMainCode",
)


class BackendError(RuntimeError):
    """A supervisor CLI is missing, refused, or returned unparsable output."""


@dataclass
class Service:
    """One supervised unit, normalised across supervisors."""

    name: str
    supervisor: str            # "pm2" | "systemd"
    state: str                 # running | stopped | errored | unknown
    restarts: int = 0
    pid: Optional[int] = None
    memory_bytes: Optional[int] = None
    cpu_percent: Optional[float] = None
    started_at: Optional[str] = None
    uptime_seconds: Optional[float] = None
    exit_code: Optional[int] = None
    cwd: Optional[str] = None
    script: Optional[str] = None
    log_path: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _run(argv: List[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run a supervisor CLI, raising BackendError with a useful message on failure."""
    if shutil.which(argv[0]) is None:
        raise BackendError(f"{argv[0]} is not installed / not on PATH")
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise BackendError(f"{' '.join(argv)} timed out after {timeout:g}s")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1] if err else f"exit {proc.returncode}"
        raise BackendError(f"{' '.join(argv)} failed: {detail}")
    return proc.stdout


# --------------------------------------------------------------------------- pm2


def pm2_available() -> bool:
    return shutil.which("pm2") is not None


def pm2_services() -> List[Service]:
    """Every pm2 process, from ``pm2 jlist``."""
    raw = _run(["pm2", "jlist"])
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise BackendError(f"pm2 jlist returned non-JSON output: {exc}")

    out: List[Service] = []
    for entry in entries:
        env = entry.get("pm2_env") or {}
        monit = entry.get("monit") or {}
        status = env.get("status") or "unknown"
        state = {
            "online": "running",
            "stopped": "stopped",
            "errored": "errored",
            "stopping": "stopped",
            "launching": "running",
            "one-launch-status": "unknown",
        }.get(status, "unknown")

        pid = entry.get("pid") or None
        if pid == 0:
            pid = None

        started_ms = env.get("pm_uptime")
        started_at = None
        uptime_s = None
        if started_ms:
            from datetime import datetime, timezone
            started_at = datetime.fromtimestamp(
                started_ms / 1000.0, tz=timezone.utc
            ).isoformat()
            import time
            uptime_s = max(0.0, time.time() - started_ms / 1000.0)

        out.append(Service(
            name=env.get("name") or entry.get("name") or "?",
            supervisor="pm2",
            state=state,
            restarts=int(env.get("restart_time") or 0),
            pid=pid,
            memory_bytes=monit.get("memory") or None,
            cpu_percent=monit.get("cpu"),
            started_at=started_at,
            uptime_seconds=uptime_s,
            exit_code=env.get("exit_code"),
            cwd=env.get("pm_cwd"),
            script=env.get("pm_exec_path"),
            log_path=env.get("pm_err_log_path") or env.get("pm_out_log_path"),
            detail={
                "pm2_status": status,
                "namespace": env.get("namespace"),
                "unstable_restarts": env.get("unstable_restarts"),
            },
        ))
    return out


# ----------------------------------------------------------------------- systemd


def systemd_available(user: bool = True) -> bool:
    if shutil.which("systemctl") is None:
        return False
    try:
        _run(_systemctl(user, ["list-units", "--type=service", "--no-pager", "--plain", "--no-legend"]))
        return True
    except BackendError:
        return False


def _systemctl(user: bool, args: Iterable[str]) -> List[str]:
    argv = ["systemctl"]
    if user:
        argv.append("--user")
    return argv + list(args)


def _ns_to_int(value: str) -> Optional[int]:
    """systemd renders size/timeouts as `123456` or `[not set]`."""
    if not value or value.startswith("["):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def systemd_services(user: bool = True, include_inactive: bool = False) -> List[Service]:
    """Load real unit files and read each unit's state in one pass per unit.

    ``systemctl list-unit-files`` is the authoritative list of *defined* units;
    ``list-units`` only shows loaded ones, so a disabled unit would otherwise be
    invisible exactly when it matters.
    """
    argv = _systemctl(user, ["list-unit-files", "--type=service", "--no-pager", "--plain", "--no-legend"])
    raw = _run(argv)

    names: List[str] = []
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        # Skip template units and generated/transient noise
        if "@" in name or name.startswith("app-"):
            continue
        if name in names:            # list-unit-files can emit the same unit twice
            continue
        names.append(name)

    out: List[Service] = []
    for name in names:
        try:
            out.extend(_systemd_show(name, user=user))
        except BackendError:
            continue
    if not include_inactive:
        out = [s for s in out if s.state in ("running", "errored")]
    return out


def _systemd_show(name: str, user: bool = True) -> List[Service]:
    props = " ".join(f"-p{p}" for p in SYSTEMD_PROPS)
    argv = _systemctl(user, ["show", name, *props.split()])
    raw = _run(argv)
    kv: Dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v

    if kv.get("LoadState") != "loaded":
        return []

    active = kv.get("ActiveState", "unknown")
    sub = kv.get("SubState", "")
    state = {"active": "running", "failed": "errored", "inactive": "stopped"}.get(active, "unknown")
    if active == "active" and sub not in ("running", "exited"):
        state = "stopped" if sub in ("dead", "exited") else "unknown"

    pid = _ns_to_int(kv.get("MainPID", ""))
    if pid == 0:
        pid = None

    started = kv.get("ExecMainStartTimestamp") or None
    uptime_s = None
    if started and pid:
        uptime_s = _systemd_uptime(started)

    service_name = kv.get("Id") or name
    return [Service(
        name=service_name,
        supervisor="systemd",
        state=state,
        restarts=_ns_to_int(kv.get("NRestarts", "")) or 0,
        pid=pid,
        memory_bytes=_ns_to_int(kv.get("MemoryCurrent", "")),
        started_at=started,
        uptime_seconds=uptime_s,
        exit_code=_ns_to_int(kv.get("ExecMainStatus", "")),
        detail={
            "active_state": active,
            "sub_state": sub,
            "result": kv.get("Result"),
            "unit_file_state": kv.get("UnitFileState"),
            "scope": "user" if user else "system",
        },
    )]


def _systemd_uptime(started: str) -> Optional[float]:
    from datetime import datetime, timezone
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(started, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
    return None


# ------------------------------------------------------------------------- merge


def collect(
    user_systemd: bool = True,
    include_systemd: bool = True,
    include_pm2: bool = True,
) -> Dict[str, Any]:
    """Read every supervisor that is present. Never raises on a missing supervisor."""
    services: List[Service] = []
    errors: List[str] = []

    if include_pm2:
        if pm2_available():
            try:
                services.extend(pm2_services())
            except BackendError as exc:
                errors.append(str(exc))
        else:
            errors.append("pm2 not installed")

    if include_systemd:
        if systemd_available(user=user_systemd):
            try:
                services.extend(systemd_services(user=user_systemd, include_inactive=True))
            except BackendError as exc:
                errors.append(str(exc))
        else:
            errors.append("systemctl unavailable for this user session")

    return {"services": services, "errors": errors}
