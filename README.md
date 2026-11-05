# hermes-nurse

Supervisor-aware service nurse for [Hermes Agent](https://hermes-agent.nousresearch.com).

Most agents can *watch* processes. `hermes-nurse` lets Hermes **manage** them:
it reads pm2 and systemd in one table, turns a dead service's logs into a
diagnosis, and repairs it under guards that stop you from turning one dead
service into an outage.

## The problem it solves

A box that runs agents runs services: pm2 processes, systemd units, tunnels,
bots. When one dies at 3am you get a restart counter and a log file. Reading
that by hand is the job — and it is exactly the job an agent should do, if it
has tools that reach the supervisor instead of only the shell.

## Tools

| Tool | What it does |
|---|---|
| `service_list` | Every pm2 process and systemd unit in one table: state, restart count, PID, memory, uptime, last exit code, crash-loop flag. Read-only. |
| `service_triage` | Diagnose a down service. Reads its recent log (pm2 error/out log, following rotation, or the systemd journal), inspects the exit code, and returns ranked findings with evidence. Read-only. |
| `service_repair` | `restart`, `start`, `stop`, `reload`, `reset-failed` — allowlisted, confirmation-gated, crash-loop aware, rate-limited. The only tool that changes state. |

### Diagnoses `service_triage` can reach

`out_of_memory` · `port_in_use` · `missing_env` · `auth_expired` ·
`dependency_unreachable` · `rate_limited` · `disk_full` · `permission_denied` ·
`module_missing` · `syntax_error` · `uncaught_exception` ·
`killed_by_signal` (exit 137/139/143) · `unstable_restarts` · `stopped_cleanly`

Each finding carries a confidence, the log lines that produced it, and a
remediation. When nothing matches, it says so instead of guessing.

## Install

```bash
hermes plugins install Megumiiiiii/hermes-nurse
hermes plugins enable nurse
```

Then make the `nurse` toolset visible on your platform. Hermes only loads
toolsets listed in `platform_toolsets`, and a plugin toolset is not added
automatically:

```bash
hermes config set platform_toolsets.cli '["browser","clarify","code_execution","cronjob","delegation","file","memory","nurse","session_search","skills","terminal","todo","tts","vision","web"]'
```

If you use tool search, note that plugin tools are deferred by default. Set
`tools.tool_search.enabled: off` (or keep the three `service_*` names eager)
or the model will have to search for them before every call.

## Guard rails (why the repair tool is safe to hand an agent)

Handing an agent `pm2 restart` is how you turn a crash into an outage. Four
rules exist for that:

1. **Allowlist only.** Verbs are `restart`/`start`/`stop`/`reload`/`reset-failed`.
   There is no "run this command" escape hatch.
2. **Crash-loop refusal.** A service restarting faster than 5 times per hour is
   refused, with the rate named. Restarting it again only burns the same
   failing path.
3. **Cooldown ledger.** One repair per service every 2 minutes, 3 per hour,
   persisted to disk so a fresh Hermes session inherits the cooldown instead of
   starting clean.
4. **Confirmation required.** The first call returns a plan; nothing changes
   until the model passes `confirm=true` after telling you what will restart.

A brand-new process is never declared unstable: pm2's restart counter is
cumulative for the entry, so a service that crashed 15 times last week but was
just restarted is healthy until its own uptime proves otherwise.

## Requirements

- Linux (systemd journal + `/proc` based checks). pm2 support is platform
  independent; systemd support requires `systemctl`/`journalctl`.
- `pm2` and/or `systemctl` on `PATH`. Both being absent is not an error — the
  tools report what they could read.
- Python standard library only. No third-party packages.

## Disclosure

- **Reads:** process state via `pm2 jlist` and `systemctl show`/`journalctl`;
  pm2 log files under `~/.pm2/logs/`; the systemd user journal. Nothing is read
  outside those sources.
- **Writes:** a restart ledger and a session snapshot under
  `$HERMES_HOME/plugin-data/nurse/` (falls back to
  `$HERMES_PLUGIN_DATA_DIR`). No other files are written.
- **Changes state:** only `service_repair`, and only for the named service,
  after a confirmation flag. It never edits unit files, config, or code.
- **Network:** none. The plugin makes no outbound calls.
- **Credentials:** it reads no secrets and has no `requires_env`.
- **User/system scope:** systemd reads user units by default. Root units require
  the caller's own privilege; the plugin does not escalate.

## Tests

```bash
python -m pytest -q             # 42 tests
```

The suite encodes bugs found against a real machine (rotated log files, the
cumulative restart counter, a crash rate over a long uptime), so it acts as a
regression fence rather than a coverage exercise.

## License

MIT
