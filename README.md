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

The restart counter is the part most tools get wrong. `pm2`'s counter is
**cumulative for the entry**: a service restarted fifteen times last week and
started fresh an hour ago still reports fifteen. Sort by it and a healthy
service outranks a dying one. `hermes-nurse` measures **restarts per hour over
the current lifetime** instead, so 13 restarts across 48 hours reads as healthy
and 47 restarts across 3 hours reads as a crash loop — which is what each
actually is.

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

Nothing else. Plugin toolsets are enabled by default on every platform, so the
three tools are available on the CLI, the messaging platforms and the desktop
app as soon as the plugin is enabled.

One Hermes-side caveat: with `tools.tool_search` left at its default, plugin
tools are deferred and the model has to *search* for them, which it does badly
when the name does not match the words in your question. Turning that off makes
them eager:

```bash
hermes config set tools.tool_search.enabled off
```

## Guard rails (why the repair tool is safe to hand an agent)

Handing an agent `pm2 restart` is how you turn a crash into an outage. Four
rules exist for that:

1. **Allowlist only.** Verbs are `restart`/`start`/`stop`/`reload`/`reset-failed`.
   There is no "run this command" escape hatch; the service name is passed as
   its own `argv` element so it never reaches a shell.
2. **Crash-loop refusal.** A service restarting faster than 5 times per hour is
   refused, with the rate named. Restarting it again only burns the same
   failing path — and it destroys the log evidence of why it was dying.
3. **Cooldown ledger.** One repair per service every 2 minutes, 3 per hour,
   persisted to disk so a fresh Hermes session inherits the cooldown instead of
   starting clean.
4. **Confirmation required.** The first call returns a plan; nothing changes
   until the model passes `confirm=true` after telling you what will restart.

A brand-new process is never declared unstable: a single restart is an event,
not a pattern, and the most common single restart is a deliberate one issued
moments ago by this very tool.

## Bugs found while building this

The suite is a regression fence, not a coverage exercise — every test below
encodes something that actually broke against a real machine, most of them
found only by running the tools for real rather than by reading the code.

1. **Handlers never ran under Hermes.** Hermes dispatches a plugin tool as
   `handler(args_dict, **kwargs)`; handlers declared with individual parameters
   received the dict in their first slot and raised `AttributeError` inside the
   domain code. The whole suite passed because tests called the functions the
   Python way. Two tests now drive the handlers exactly as the registry does.
2. **Every successful repair crashed.** `_clean_output()` returns a list of
   relevant lines, and the caller then called `.strip()` on it.
3. **Two definitions of "unstable" in one tool.** The list filter used the
   cumulative count while the same row printed `crash_loop: false`. Both now
   call the same predicate, so a row that was filtered in is necessarily
   flagged.
4. **One restart was reported as a crash loop.** A gateway restarted once, 220
   seconds after launch, reads as 16 restarts/hour. A minimum of two restarts
   removes the false positive without weakening the real case.
5. **Duplicate service rows.** `systemctl list-unit-files` can emit the same
   unit twice; rows are deduplicated by `(supervisor, name)`.
6. **`severity` was buried in `findings[0]`.** Callers had to reach inside the
   first finding to learn how bad a service is; it is now a top-level field.

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
python -m pytest -q             # 45 tests
```

## License

MIT
