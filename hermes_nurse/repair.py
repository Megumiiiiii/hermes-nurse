"""Repair: the only mutating surface in hermes-nurse.

Design rules that exist because restart loops are how you turn one dead
service into an outage:

1. Every verb is **allowlisted** — there is no "run this command" escape hatch.
2. A service in a crash loop is **refused**, not obediently restarted.
3. Restarts are rate-limited per service with a persisted ledger, so an agent
   in a retry loop cannot hammer a service 40 times in a minute.
4. State survives process restarts, so a fresh Hermes session inherits the
   cooldown instead of starting with a clean slate.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .backends import BackendError, Service, _run, _systemctl

# Allowed repair verbs per supervisor. Anything else is refused.
PM2_VERBS = {"restart", "reload", "stop", "start", "reset"}
SYSTEMD_VERBS = {"restart", "start", "stop", "reset-failed"}

# Refuse a restart when restarts exceed this rate over the service's lifetime,
# or when this many restarts happened inside CRASH_LOOP_UPTIME seconds.
CRASH_LOOP_RESTARTS = 10
CRASH_LOOP_UPTIME = 600           # seconds
CRASH_LOOP_PER_HOUR = 5.0         # restarts/hour above this = unstable
# A rate alone libels a young process: one manual restart 200s after launch
# reads as 18/hour. Require a second restart before calling anything unstable —
# a real crash loop reaches this within seconds, a deliberate restart never does.
CRASH_LOOP_MIN_RESTARTS = 2
COOLDOWN_SECONDS = 120            # minimum gap between agent-issued restarts
MAX_ATTEMPTS_PER_HOUR = 3


def _data_dir() -> str:
    """Profile-scoped plugin data dir, falling back to a safe local path."""
    env = os.environ.get("HERMES_PLUGIN_DATA_DIR")
    if env:
        path = env
    else:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        path = os.path.join(home, "plugin-data", "nurse")
    os.makedirs(path, exist_ok=True)
    return path


def _ledger_path() -> str:
    return os.path.join(_data_dir(), "restart-ledger.json")


def load_ledger() -> Dict[str, List[float]]:
    try:
        with open(_ledger_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: [float(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError):
        pass
    return {}


def save_ledger(ledger: Dict[str, List[float]]) -> None:
    tmp = _ledger_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
    os.replace(tmp, _ledger_path())


def _key(service: Service) -> str:
    return f"{service.supervisor}:{service.name}"


def _restart_rate(service: Service) -> Optional[float]:
    """Restarts per hour over the service's current lifetime, or None if unknown.

    Uptime alone is the wrong signal: a service that dies every two hours has an
    uptime far above any "crash loop" threshold while still being unstable. The
    rate catches both shapes — 47 restarts over 1.4h is ~33/h.
    """
    # A service restarted seconds ago divides by nearly zero and reports an
    # absurd rate. Below a minute of life the sample is meaningless, so treat the
    # rate as unknown and let the short-uptime/restart-count branch decide.
    if not service.uptime_seconds or service.uptime_seconds < 60:
        return None
    return service.restarts / (service.uptime_seconds / 3600.0)


def is_crash_loop(service: Service) -> bool:
    """True when the *current* lifetime shows the service cannot stay up.

    ``restart_time`` is cumulative for the life of the pm2 entry — a service that
    crashed 15 times last week and was just started fresh still reports 15. That
    number alone must never block a repair; only restarts that outpace the uptime
    of the run in front of us do.
    """
    if service.restarts < CRASH_LOOP_MIN_RESTARTS:
        # One restart is an event, not a pattern — and the most common single
        # restart is a deliberate one issued moments ago by this very tool.
        return False
    rate = _restart_rate(service)
    if rate is None:
        # Under a minute of life the sample is meaningless either way (pm2's
        # restart_time is cumulative for the entry, not for this process), so a
        # brand-new process must never be declared unstable.
        return False
    return rate >= CRASH_LOOP_PER_HOUR


@dataclass
class GuardVerdict:
    allowed: bool
    reason: str = ""
    needs_confirmation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason,
                "needs_confirmation": self.needs_confirmation}


def check_guard(service: Service, verb: str, confirm: bool = False) -> GuardVerdict:
    """Decide whether a repair may run *before* anything is executed."""
    if verb not in PM2_VERBS | SYSTEMD_VERBS:
        return GuardVerdict(False, f"verb '{verb}' is not in the allowlist")
    allowed_verbs = PM2_VERBS if service.supervisor == "pm2" else SYSTEMD_VERBS
    if verb not in allowed_verbs:
        return GuardVerdict(False, f"verb '{verb}' is not supported by {service.supervisor}")

    if verb in ("restart", "start"):
        now = time.time()
        history = load_ledger().get(_key(service), [])
        recent = [t for t in history if now - t < 3600]
        if recent and now - recent[-1] < COOLDOWN_SECONDS:
            wait = int(COOLDOWN_SECONDS - (now - recent[-1]))
            return GuardVerdict(
                False,
                f"cooling down: {service.name} was repaired "
                f"{int(now - recent[-1])}s ago; wait {wait}s before trying again.",
            )

        if is_crash_loop(service):
            rate = _restart_rate(service)
            rate_txt = f"{rate:.1f} restarts/hour" if rate is not None else "many restarts"
            return GuardVerdict(
                False,
                f"refusing to restart {service.name}: unstable ({service.restarts} "
                f"restarts, {rate_txt}). Fix the root cause from service_triage first.",
            )

        if len(recent) >= MAX_ATTEMPTS_PER_HOUR:
            return GuardVerdict(
                False,
                f"attempt budget exhausted: {len(recent)} repairs in the last hour "
                f"(max {MAX_ATTEMPTS_PER_HOUR}). This is a persistent fault, not a hiccup.",
            )

        if not confirm:
            return GuardVerdict(
                True,
                f"{verb} {service.supervisor} service {service.name} — confirmation required",
                needs_confirmation=True,
            )

    return GuardVerdict(True, "ok")


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_output(text: str, service_name: str) -> List[str]:
    """Strip ANSI and keep only the lines that mention the service.

    pm2 answers with a full coloured process table; handing that to the model
    wastes tokens and buries the one line that matters.
    """
    plain = _ANSI.sub("", text or "")
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    named = [ln for ln in lines if service_name in ln]
    return (named or lines)[-5:]


def record_repair(service: Service) -> None:
    ledger = load_ledger()
    key = _key(service)
    now = time.time()
    history = [t for t in ledger.get(key, []) if now - t < 3600]
    history.append(now)
    ledger[key] = history
    save_ledger(ledger)


def repair(service: Service, verb: str = "restart", confirm: bool = False) -> Dict[str, Any]:
    """Execute an allowlisted repair verb against a collected Service."""
    verdict = check_guard(service, verb, confirm=confirm)
    if not verdict.allowed:
        return {"ok": False, "blocked": True, "reason": verdict.reason,
                "service": service.name, "verb": verb}
    if verdict.needs_confirmation:
        return {"ok": False, "needs_confirmation": True, "reason": verdict.reason,
                "service": service.name, "verb": verb,
                "hint": "Call again with confirm=true after telling the user what will restart."}

    if service.supervisor == "pm2":
        argv = ["pm2", verb, service.name]
        if verb == "reset":
            argv = ["pm2", "reset", service.name]
    else:
        user = (service.detail or {}).get("scope", "user") == "user"
        if verb == "reset-failed":
            argv = _systemctl(user, ["reset-failed", service.name])
        else:
            # --no-block so a slow unit cannot hold the tool call open
            argv = _systemctl(user, [verb, service.name, "--no-block"])

    try:
        out = _clean_output(_run(argv, timeout=25), service.name)
    except BackendError as exc:
        return {"ok": False, "blocked": False, "reason": str(exc),
                "service": service.name, "verb": verb}

    record_repair(service)
    return {
        "ok": True,
        "service": service.name,
        "supervisor": service.supervisor,
        "verb": verb,
        # _clean_output already returns a list of relevant lines — calling
        # .strip() on it raised AttributeError on every successful repair.
        "output": out or [],
        "note": "verify with service_list — a restart that immediately dies again means "
                "the root cause is still there",
    }
