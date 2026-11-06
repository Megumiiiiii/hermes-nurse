"""Tool schemas — exactly what the model reads when choosing a tool."""

SERVICE_LIST = {
    "name": "service_list",
    "description": (
        "List every supervised service on this machine in one table: pm2 processes "
        "and systemd units together, with state, restart count, PID, memory, uptime "
        "and last exit code. Use this first whenever the user asks what is running, "
        "why something is down, or before repairing anything. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "only_problems": {
                "type": "boolean",
                "description": "Return only services that are stopped, errored, or have a high restart count.",
            },
            "supervisor": {
                "type": "string",
                "enum": ["all", "pm2", "systemd"],
                "description": "Restrict the listing to one supervisor. Default all.",
            },
            "include_system_units": {
                "type": "boolean",
                "description": "Also read root systemd units (needs privilege). Default false: user units only.",
            },
        },
    },
}

SERVICE_TRIAGE = {
    "name": "service_triage",
    "description": (
        "Diagnose why a supervised service is down or unhealthy. Reads its recent log "
        "(pm2 error/out log or systemd journal), inspects the exit code, and returns a "
        "ranked diagnosis with evidence: out_of_memory, port_in_use, missing_env, "
        "auth_expired, dependency_unreachable, rate_limited, disk_full, "
        "permission_denied, module_missing, syntax_error, crash_loop. Use this before "
        "restarting anything — restarting a service with a live crash loop wastes the "
        "evidence. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service name (pm2 process name, or systemd unit with or without the .service suffix).",
            },
            "log_lines": {
                "type": "integer",
                "description": "How many recent log lines to examine. Default 120.",
            },
        },
        "required": ["service"],
    },
}

SERVICE_REPAIR = {
    "name": "service_repair",
    "description": (
        "Repair a supervised service: restart, start, stop, reload (pm2 only) or "
        "reset-failed (systemd only). Guarded: every state-changing verb requires "
        "confirm=true (the first call returns a plan) and is escalated to Hermes' "
        "human approval prompt, plus refuses services in a crash loop and "
        "rate-limits to one repair per service every 2 minutes and 3 per hour. "
        "Always run service_triage first so the repair targets the real cause."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service name (pm2 process name, or systemd unit with or without the .service suffix).",
            },
            "action": {
                "type": "string",
                "enum": ["restart", "start", "stop", "reload", "reset-failed"],
                "description": "Repair verb. Default restart.",
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true to actually change state. The first call without it returns a plan instead of acting.",
            },
        },
        "required": ["service"],
    },
}
