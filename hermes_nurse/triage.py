"""Crash triage: turn a dead service's logs + exit code into a diagnosis.

This is the part a skill cannot do. A skill can *describe* how to read
``pm2 logs``; only real code can tail both supervisors, match the output
against a fingerprint registry, and hand back a ranked cause with evidence.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from .backends import BackendError, Service, _run, _systemctl
from .repair import is_crash_loop, _restart_rate

# Exit codes that map to a known cause regardless of log text
SIGNAL_EXIT: Dict[int, str] = {
    129: "SIGHUP",
    130: "SIGINT",
    134: "SIGABRT",
    137: "SIGKILL (often OOM killer)",
    139: "SIGSEGV (segfault)",
    143: "SIGTERM",
}


@dataclass
class Finding:
    """One suspected cause with its evidence."""

    cause: str
    severity: str            # "critical" | "warning" | "info"
    explanation: str
    confidence: float        # 0..1
    evidence: List[str] = field(default_factory=list)
    remediation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Fingerprint:
    cause: str
    severity: str
    pattern: re.Pattern
    explanation: str
    remediation: str
    weight: float = 0.7


def _fp(cause, severity, regex, explanation, remediation, weight=0.7) -> Fingerprint:
    return Fingerprint(cause, severity, re.compile(regex, re.IGNORECASE), explanation, remediation, weight)


# Ordered most-specific-first: the first strong match wins the headline.
FINGERPRINTS: List[Fingerprint] = [
    _fp(
        "out_of_memory", "critical",
        r"JavaScript heap out of memory|FATAL ERROR: Reached heap limit|"
        r"Cannot allocate memory|MemoryError|Killed process \d+|"
        r"out of memory|OOMKilled|bad_alloc",
        "The process died because it ran out of memory — the allocator or the "
        "kernel's OOM killer terminated it.",
        "Raise the service memory limit, or add pm2 --max-memory-restart / a "
        "systemd MemoryHigh cap so it recycles before the host OOM killer fires.",
        0.95,
    ),
    _fp(
        "port_in_use", "critical",
        r"EADDRINUSE|address already in use|bind: Address already in use|"
        r"listen EADDRINUSE",
        "The service could not bind its port because another process already "
        "holds it — usually an orphan of the previous run.",
        "Find the holder (ss -lptn 'sport = :PORT'), stop it, then start the "
        "service again. A crash loop that restarts fast enough can also race "
        "its own socket release.",
        0.95,
    ),
    _fp(
        "missing_env", "critical",
        r"Missing required env|environment variable .* (?:is )?not set|"
        r"KeyError: '(?:[A-Z_]{3,})'|Unauthorized: no credentials|"
        r"API key (?:is )?(?:missing|not configured)",
        "A required environment variable or credential is absent from the "
        "supervisor's environment, so the process refused to start.",
        "Check the pm2 ecosystem file / systemd Environment= or EnvironmentFile= "
        "and confirm the variable reaches the process, not just your shell.",
        0.9,
    ),
    _fp(
        "auth_expired", "critical",
        r"401 Unauthorized|403 Forbidden|token (?:has )?expired|"
        r"invalid[_ ]api[_ ]key|signature verification failed",
        "The service reached its dependency but was rejected — a credential is "
        "expired, rotated or scoped wrong.",
        "Rotate or refresh the credential, then restart. Repeated retries will "
        "not help; the failure is deterministic.",
        0.85,
    ),
    _fp(
        "dependency_unreachable", "warning",
        r"ECONNREFUSED|ETIMEDOUT|ENOTFOUND|getaddrinfo|"
        r"Temporary failure in name resolution|Connection refused|"
        r"upstream connect error",
        "The service could not reach something it depends on: a database, an API, "
        "an RPC endpoint or DNS.",
        "Verify the dependency is up and resolvable from this host before "
        "blaming the service. If the dependency is flapping, fix it there.",
        0.8,
    ),
    _fp(
        "rate_limited", "warning",
        r"429 Too Many Requests|rate limit|Too many requests|"
        r"slow down|quota exceeded",
        "The service is being throttled by the provider it calls.",
        "Add backoff or lower concurrency. Restarting alone will not clear a "
        "provider-side quota window.",
        0.8,
    ),
    _fp(
        "disk_full", "critical",
        r"ENOSPC|No space left on device|disk quota exceeded",
        "The filesystem holding the service's data is full.",
        "Free space (logs are the usual culprit) before restarting, or the "
        "service will die again on the next write.",
        0.95,
    ),
    _fp(
        "permission_denied", "critical",
        r"EACCES|Permission denied|Operation not permitted|"
        r"read-only file system",
        "The process tried to touch a file or socket it may not access.",
        "Compare the supervisor's user with the file owner; fix ownership rather "
        "than running the service as root.",
        0.85,
    ),
    _fp(
        "module_missing", "critical",
        r"Cannot find module|ModuleNotFoundError|ImportError: No module named|"
        r"command not found",
        "A runtime dependency is missing from the interpreter or PATH the "
        "supervisor uses — often a different venv/node_modules than your shell.",
        "Install into the supervisor's own interpreter (pm2 interpreter / "
        "systemd ExecStart path), not into whatever is active in your terminal.",
        0.9,
    ),
    _fp(
        "syntax_error", "critical",
        r"SyntaxError|Unexpected token|Unexpected end of input|"
        r"IndentationError",
        "The entrypoint does not parse — the process never got as far as running.",
        "Run the file through the interpreter directly to see the line; a partial "
        "deploy or a half-written file is the usual cause.",
        0.9,
    ),
    _fp(
        "uncaught_exception", "warning",
        r"UnhandledPromiseRejection|Unhandled 'error' event|"
        r"Traceback \(most recent call last\)|TypeError:|ValueError:",
        "The process reached an unhandled exception and exited.",
        "Read the top of the stack trace, not the bottom: the first frame in your "
        "own code is where the fix belongs.",
        0.6,
    ),
]


def tail_file(path: str, lines: int = 120, max_bytes: int = 200_000) -> str:
    """Read the last ``lines`` lines of a file without loading a huge log."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-lines:])


def journal_tail(unit: str, lines: int = 120, user: bool = True, since: str = "-2 hours") -> str:
    """Read a systemd unit's journal; degrades quietly when journald is absent."""
    if shutil.which("journalctl") is None:
        return ""
    argv = ["journalctl"]
    if user:
        argv.append("--user")
    argv += ["-u", unit, "-n", str(lines), "--no-pager", "-o", "cat", "--since", since]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _rotated_siblings(path: str) -> List[str]:
    """pm2-logrotate renames the live log and starts a new empty one.

    A crash that happened before the last rotation leaves ``<name>.log`` at 0
    bytes with the evidence sitting in ``<name>__<timestamp>.log``. Reading only
    the live path therefore loses exactly the incident we are asked about.
    """
    if not path:
        return []
    directory, base = os.path.split(path)
    stem, ext = os.path.splitext(base)          # ("app-error", ".log")
    prefix = stem + "__"
    try:
        entries = os.listdir(directory or ".")
    except OSError:
        return []
    candidates = [
        os.path.join(directory, name)
        for name in entries
        if name.startswith(prefix) and name.endswith(ext)
    ]
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates


def tail_log(path: str, lines: int = 120) -> str:
    """Tail a log, falling back to the newest rotated file when it is empty."""
    text = tail_file(path, lines=lines)
    if text.strip():
        return text
    for alt in _rotated_siblings(path)[:2]:
        alt_text = tail_file(alt, lines=lines)
        if alt_text.strip():
            return f"[from rotated log {os.path.basename(alt)}]\n{alt_text}"
    return ""


def _service_log(service: Service) -> str:
    """Best available log text for a service, from its own supervisor."""
    if service.supervisor == "pm2":
        chunks = []
        if service.log_path:
            chunks.append(tail_log(service.log_path))
        # pm2 keeps stdout and stderr in separate files; the interesting crash
        # text is usually in stderr, but a clean assertion often lands in out.log
        out_guess = (service.log_path or "").replace("-error.log", "-out.log")
        if out_guess != service.log_path:
            chunks.append(tail_log(out_guess))
        return "\n".join(c for c in chunks if c)
    if service.supervisor == "systemd":
        user = (service.detail or {}).get("scope", "user") == "user"
        return journal_tail(service.name, user=user)
    return ""


def diagnose(service: Service, extra_log: Optional[str] = None) -> Dict[str, Any]:
    """Fingerprint a service's recent logs and exit code into ranked findings."""
    log = _service_log(service)
    if extra_log:
        log = f"{extra_log}\n{log}"

    findings: List[Finding] = []

    # 1. Exit-code facts first: they are unambiguous when present.
    code = service.exit_code
    if code in SIGNAL_EXIT and code not in (0, None):
        sig = SIGNAL_EXIT[code]
        if code == 137:
            findings.append(Finding(
                cause="killed_by_signal",
                severity="critical",
                explanation="Exit 137 means the process was SIGKILLed — the kernel OOM "
                            "killer or an operator did it. A service that cannot trap "
                            "SIGTERM and exits 143 is a different bug.",
                confidence=0.9,
                evidence=[f"exit_code={code} ({sig})"],
                remediation="Check dmesg for 'Killed process' to confirm the OOM "
                            "killer, then cap memory.",
            ))

    # 2. Log fingerprints
    log_lower_lines = [ln.strip() for ln in log.splitlines() if ln.strip()]
    for fp in FINGERPRINTS:
        hits = [ln for ln in log_lower_lines if fp.pattern.search(ln)]
        if not hits:
            continue
        findings.append(Finding(
            cause=fp.cause,
            severity=fp.severity,
            explanation=fp.explanation,
            confidence=fp.weight,
            evidence=hits[-3:],
            remediation=fp.remediation,
        ))

    # 3. Instability is its own cause: restarts per hour catches the slow-failure
    #    shape (dies every 2h) that a short-uptime check misses entirely.
    if is_crash_loop(service):
        rate = _restart_rate(service)
        findings.append(Finding(
            cause="unstable_restarts",
            severity="critical",
            explanation=f"{service.restarts} restarts over "
                        f"{int(service.uptime_seconds or 0)}s of lifetime"
                        + (f" ({rate:.1f} per hour)" if rate is not None else "")
                        + " — the supervisor keeps bringing back a process that does not stay up.",
            confidence=0.95,
            evidence=[f"restarts={service.restarts}",
                      f"uptime={int(service.uptime_seconds or 0)}s",
                      f"rate_per_hour={rate:.2f}" if rate is not None else "rate=unknown"],
            remediation="Stop the loop before restarting: fix the root cause first, or "
                        "each restart burns the same failing path again.",
        ))

    # 4. Silently stopped with zero restarts is a different failure mode
    if service.state == "stopped" and service.restarts == 0 and not findings:
        findings.append(Finding(
            cause="stopped_cleanly",
            severity="warning",
            explanation="The service is stopped with no restarts and no error output — "
                        "it was stopped deliberately, or its start condition never held.",
            confidence=0.6,
            evidence=["state=stopped", "restarts=0"],
            remediation="Confirm whether it should be running at all before restarting; "
                        "a deliberately stopped service that comes back on every check is "
                        "its own incident.",
        ))

    findings.sort(key=lambda f: (-f.confidence, f.severity != "critical"))

    headline = findings[0].cause if findings else "unknown"
    return {
        "service": service.name,
        "supervisor": service.supervisor,
        "state": service.state,
        "exit_code": service.exit_code,
        "restarts": service.restarts,
        "uptime_seconds": service.uptime_seconds,
        "headline": headline,
        "findings": [f.to_dict() for f in findings],
        "log_lines_examined": len(log_lower_lines),
        "log_excerpt": log_lower_lines[-25:],
        "verdict": _verdict(findings, service),
    }


def _verdict(findings: List[Finding], service: Service) -> str:
    if not findings:
        if service.state == "running":
            return "Healthy as far as the logs show — nothing matching the crash registry."
        return ("No known crash pattern in the recent log window. Widen the window or "
                "read the full log before drawing a conclusion.")
    top = findings[0]
    return f"{top.cause}: {top.explanation} → {top.remediation}"


def find_service(services: List[Service], name: str) -> Optional[Service]:
    """Resolve a human-typed service name to a collected Service.

    Accepts an exact match, a systemd unit without its ``.service`` suffix, and
    a unique case-insensitive prefix.
    """
    if not name:
        return None
    target = name.strip()
    for s in services:
        if s.name == target:
            return s
    stripped = target[:-8] if target.endswith(".service") else target
    for s in services:
        base = s.name[:-8] if s.name.endswith(".service") else s.name
        if base == stripped:
            return s
    low = stripped.lower()
    matches = [s for s in services if s.name.lower().startswith(low)]
    return matches[0] if len(matches) == 1 else None
