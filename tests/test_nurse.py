"""Unit tests for hermes-nurse.

Every case here encodes a bug that was observed against a real machine, so the
suite is a regression fence rather than a coverage exercise. No test shells out
to pm2/systemctl: supervisor output is fed in as fixtures.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_nurse import backends, repair, triage  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own ledger + snapshot dir."""
    monkeypatch.setenv("HERMES_PLUGIN_DATA_DIR", str(tmp_path))
    yield tmp_path


def svc(**kwargs):
    base = dict(name="app", supervisor="pm2", state="stopped", restarts=0,
                pid=None, uptime_seconds=None, exit_code=None)
    base.update(kwargs)
    return backends.Service(**base)


# --------------------------------------------------------------- crash detection

def test_slow_failure_is_detected_by_rate():
    """47 restarts over 1.4h is 33/h: unstable, even though uptime > 10 minutes.

    This is the shape a short-uptime-only check misses completely.
    """
    s = svc(state="running", pid=1234, restarts=47, uptime_seconds=1.4 * 3600)
    assert repair._restart_rate(s) == pytest.approx(33.6, abs=0.5)
    assert repair.is_crash_loop(s) is True


def test_long_stable_service_is_not_unstable():
    s = svc(state="running", pid=1234, restarts=2, uptime_seconds=72 * 3600)
    assert repair.is_crash_loop(s) is False


def test_young_process_with_stale_restart_count_is_not_blocked():
    """A fresh process inherits the pm2 entry's cumulative restart count.

    Blocking it would make every manual restart of a previously-flaky service
    impossible, so a young process with no measurable rate must pass.
    """
    s = svc(state="running", pid=1234, restarts=15, uptime_seconds=19)
    assert repair._restart_rate(s) is None
    verdict = repair.check_guard(s, "restart", confirm=True)
    assert verdict.allowed is True


# ------------------------------------------------------------------- allowlist

@pytest.mark.parametrize("verb", ["rm -rf /", "restart; rm -rf /", "exec", "", None])
def test_unknown_verbs_are_refused(verb):
    verdict = repair.check_guard(svc(), verb, confirm=True)
    assert verdict.allowed is False


def test_systemd_refuses_pm2_only_verb():
    verdict = repair.check_guard(svc(supervisor="systemd", name="x.service"), "reload", confirm=True)
    assert verdict.allowed is False
    assert "not supported" in verdict.reason


# --------------------------------------------------------------------- cooldown

def test_second_repair_is_cooled_down(tmp_path):
    s = svc(state="running", pid=1, uptime_seconds=3600, restarts=0)
    repair.record_repair(s)
    verdict = repair.check_guard(s, "restart", confirm=True)
    assert verdict.allowed is False
    assert "cooling down" in verdict.reason


def test_cooldown_survives_process_restart(tmp_path):
    """The ledger is on disk: a new Hermes session must inherit the cooldown."""
    s = svc(state="running", pid=1, uptime_seconds=3600, restarts=0)
    repair.record_repair(s)
    reloaded = json.loads((tmp_path / "restart-ledger.json").read_text())
    assert reloaded["pm2:app"]
    verdict = repair.check_guard(s, "restart", confirm=True)
    assert verdict.allowed is False


def test_attempt_budget_exhausted(tmp_path):
    s = svc(state="running", pid=1, uptime_seconds=3600, restarts=0)
    now = time.time()
    repair.save_ledger({"pm2:app": [now - 3000, now - 2000, now - 1000]})
    verdict = repair.check_guard(s, "restart", confirm=True)
    assert verdict.allowed is False
    assert "budget" in verdict.reason


def test_needs_confirmation_without_confirm_flag():
    s = svc(state="stopped", restarts=0, uptime_seconds=7200)
    verdict = repair.check_guard(s, "restart", confirm=False)
    assert verdict.allowed is True
    assert verdict.needs_confirmation is True


# ------------------------------------------------------------------- triage

@pytest.mark.parametrize("logline,expected", [
    ("FATAL ERROR: Reached heap limit Allocation failed", "out_of_memory"),
    ("Error: listen EADDRINUSE: address already in use :::3000", "port_in_use"),
    ("ENOSPC: no space left on device, write", "disk_full"),
    ("429 Too Many Requests", "rate_limited"),
    ("Error: connect ECONNREFUSED 127.0.0.1:5432", "dependency_unreachable"),
    ("Cannot find module 'dotenv'", "module_missing"),
    ("EACCES: permission denied, open '/root/x'", "permission_denied"),
    ("401 Unauthorized: token has expired", "auth_expired"),
    ("SyntaxError: Unexpected token '}'", "syntax_error"),
])
def test_fingerprints(monkeypatch, logline, expected):
    monkeypatch.setattr(triage, "_service_log", lambda service: logline)
    result = triage.diagnose(svc(supervisor="pm2", log_path=None, state="errored"))
    causes = [f["cause"] for f in result["findings"]]
    assert expected in causes


@pytest.mark.parametrize("line", [
    "KeyError: 'SOLANA_RPC_URL'",
    "Missing required environment: DATABASE_URL",
    "Environment variable DATABASE_URL is not set",
])
def test_missing_env_var_detected(monkeypatch, line):
    """All three spellings must reach missing_env — the regex was narrowed once
    because a scanner read its prose form as an environment dump."""
    monkeypatch.setattr(triage, "_service_log", lambda service: line)
    result = triage.diagnose(svc(state="errored"))
    assert result["headline"] == "missing_env"


def test_exit_137_maps_to_kill(monkeypatch):
    monkeypatch.setattr(triage, "_service_log", lambda service: "")
    result = triage.diagnose(svc(state="errored", exit_code=137))
    causes = [f["cause"] for f in result["findings"]]
    assert "killed_by_signal" in causes


def test_clean_stop_reports_stopped_cleanly(monkeypatch):
    monkeypatch.setattr(triage, "_service_log", lambda service: "")
    result = triage.diagnose(svc(state="stopped", restarts=0, exit_code=0))
    assert result["headline"] == "stopped_cleanly"


def test_healthy_service_has_no_findings(monkeypatch):
    monkeypatch.setattr(triage, "_service_log",
                        lambda service: "listening on 3000\nserved 42 requests")
    result = triage.diagnose(svc(state="running", pid=1, restarts=0,
                                 uptime_seconds=7200))
    assert result["findings"] == []
    assert "Healthy" in result["verdict"]


# --------------------------------------------------------- rotated log fallback

def test_rotated_log_is_read_when_live_log_is_empty(tmp_path):
    """pm2-logrotate leaves an empty live log and the evidence in the archive."""
    logs = tmp_path / "logs"
    logs.mkdir()
    live = logs / "app-error.log"
    live.write_text("")
    rotated = logs / "app-error__2026-11-05_00-00-00.log"
    rotated.write_text("\n".join(["boot ok", "FATAL ERROR: Reached heap limit"]) + "\n")

    text = triage.tail_log(str(live))
    assert "Reached heap limit" in text
    assert "rotated log" in text


# --------------------------------------------------------------- name resolution

def test_find_service_accepts_unit_without_suffix():
    services = [svc(name="hermes-gateway.service", supervisor="systemd")]
    assert triage.find_service(services, "hermes-gateway") is not None


def test_find_service_rejects_ambiguous_prefix():
    services = [svc(name="agent-a"), svc(name="agent-b")]
    assert triage.find_service(services, "agent") is None


# ------------------------------------------------------------------- parsing

def test_pm2_jlist_parsing(monkeypatch):
    payload = [{
        "pid": 4321,
        "name": "bot",
        "pm2_env": {
            "name": "bot", "status": "online", "restart_time": 3,
            "pm_uptime": int((time.time() - 3600) * 1000), "exit_code": 0,
            "pm_cwd": "/srv/bot", "pm_exec_path": "/srv/bot/index.js",
            "pm_err_log_path": "/tmp/bot-error.log",
        },
        "monit": {"memory": 1024 * 1024, "cpu": 4.5},
    }]
    monkeypatch.setattr(backends, "_run", lambda argv, timeout=0: json.dumps(payload))
    services = backends.pm2_services()
    assert len(services) == 1
    s = services[0]
    assert s.name == "bot" and s.state == "running"
    assert s.restarts == 3 and s.memory_bytes == 1024 * 1024
    assert s.pid == 4321


def test_pm2_stopped_pid_zero_becomes_none(monkeypatch):
    payload = [{"pid": 0, "name": "x", "pm2_env": {"name": "x", "status": "stopped"}}]
    monkeypatch.setattr(backends, "_run", lambda argv, timeout=0: json.dumps(payload))
    assert backends.pm2_services()[0].pid is None


def test_systemd_ns_to_int_handles_unset():
    assert backends._ns_to_int("[not set]") is None
    assert backends._ns_to_int("") is None
    assert backends._ns_to_int("1024") == 1024


def test_systemd_show_parses_unit(monkeypatch):
    out = (
        "Id=hermes-gateway.service\nActiveState=active\nSubState=running\n"
        "NRestarts=2\nMainPID=999\nMemoryCurrent=2048\n"
        "ExecMainStartTimestamp=\nResult=success\nUnitFileState=enabled\n"
        "LoadState=loaded\nExecMainStatus=0\n"
    )
    monkeypatch.setattr(backends, "_run", lambda argv, timeout=0: out)
    services = backends._systemd_show("hermes-gateway.service")
    assert services[0].state == "running"
    assert services[0].pid == 999
    assert services[0].restarts == 2


def test_session_snapshot_detects_service_that_died(monkeypatch, tmp_path):
    import importlib
    plugin = importlib.import_module("hermes_nurse")
    states = iter([
        {"pm2:bot": "running", "pm2:other": "running"},
        {"pm2:bot": "stopped", "pm2:other": "running"},
    ])
    monkeypatch.setattr(plugin, "_current_states", lambda: next(states))
    plugin._on_session_start()
    plugin._on_session_start()
    snap = json.loads((tmp_path / "last-snapshot.json").read_text())
    assert snap["went_down"] == ["pm2:bot"]


def test_triage_exposes_severity_and_unstable(monkeypatch):
    """Callers must not have to reach into findings[0] for severity, and the
    crash-loop flag must agree with the guard."""
    monkeypatch.setattr(triage, "_service_log",
                        lambda service: "Error: ECONNREFUSED 127.0.0.1:6379")
    result = triage.diagnose(svc(state="running", restarts=30, uptime_seconds=60))
    # A crash loop outranks the log fingerprint that happens to come with it:
    # 30 restarts in 60s is the finding that matters, not the ECONNREFUSED line.
    assert result["headline"] == "unstable_restarts"
    assert result["severity"] == "critical"
    assert result["unstable"] is True


def test_triage_healthy_service_reports_none_severity(monkeypatch):
    monkeypatch.setattr(triage, "_service_log", lambda service: "all good")
    result = triage.diagnose(svc(state="running", restarts=0, uptime_seconds=9999))
    assert result["severity"] == "none"
    assert result["unstable"] is False
