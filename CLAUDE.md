# CLAUDE.md — devtunnel project guide

## What This Is

devtunnel is a CLI + web UI tool for exposing local dev projects via Cloudflare Tunnel or Tailscale. It manages tunnel routes, Cloudflare Access policies, systemd app services, structured logging, and health checks.

## Project Structure

```
bin/devtunnel                          Bash CLI (single file, ~900 lines)
web/app.py                             Flask web UI backend
web/templates/index.html               Single-page web UI (vanilla JS)
systemd/cloudflared.service            Cloudflare tunnel systemd unit
systemd/devtunnel-web.service          Web UI systemd unit
systemd/devtunnel-healthcheck.service  Health check runner (oneshot)
systemd/devtunnel-healthcheck.timer    60s health check timer
install.sh                             Interactive installer
```

### MCP Plugin (out-of-tree)

The MCP plugin lives at `~/.claude/plugins/cache/devtunnel-local/devtunnel/1.0.0/server/src/index.ts`. It exposes devtunnel functionality as MCP tools. Most tools delegate to the CLI via `cli()` helper; keep it that way so logging and auto-restart behavior stay consistent.

## Key State Files (runtime, not in repo)

- `~/.cloudflared/devtunnel.json` — project state (ports, access, provider, dir, cmd, app IDs)
- `~/.cloudflared/config.yml` — generated cloudflared ingress config
- `~/.local/share/devtunnel/logs/devtunnel.log` — JSONL operations log (5MB rotation)
- `~/.local/share/devtunnel/healthcheck-state.json` — per-project failure counters

## Architecture Patterns

### CLI (`bin/devtunnel`)
- Bash script, `set -euo pipefail`
- State managed via `jq` atomic read-modify-write (`jq ... > tmp && mv tmp state`)
- Helpers: `die()`, `info()`, `warn()`, `log_event()`, `restart_tunnel_service()`
- App services are systemd user units created from a template in `create_app_service()`
- `log_event level action [project] [msg]` writes JSONL to the shared log file
- `restart_tunnel_service()` is the single place that restarts cloudflared — all mutation commands call it

### Web UI (`web/app.py`)
- Flask, no external dependencies beyond Flask
- Same state file as CLI — reads/writes `~/.cloudflared/devtunnel.json`
- `log_event(level, action, project, msg)` — Python equivalent of CLI logging
- API endpoints mirror CLI commands: POST add, DELETE remove, PATCH update, service control
- Template is a single HTML file with inline CSS and vanilla JS (no build step)

### Health Checks
- `devtunnel healthcheck` is a CLI command (not in help text, meant for the timer)
- Probes each running project's port with `curl -sf --max-time 3`
- 2 consecutive failures → automatic service restart
- State tracked in `healthcheck-state.json`, exposed in `/api/status` as `unhealthy_projects`

### Logging
- JSONL format, single file, shared by CLI (`source: "cli"`), web (`source: "web"`), and healthcheck (`source: "healthcheck"` via CLI)
- 5MB rotation with 1 backup
- `devtunnel log [N]` reads the file; web UI has an Operations Log tab reading `/api/operations-log`

## Build & Test

There is no build step for the CLI or web UI. The CLI is a standalone bash script. The web UI requires `flask` (`pip install flask`).

For the MCP plugin (`server/src/index.ts`): `npm run build` in the plugin directory compiles TypeScript.

To validate changes:
```bash
bash -n bin/devtunnel                    # check CLI syntax
python3 -m py_compile web/app.py         # check web syntax
```

## Important Conventions

- When adding new CLI commands that mutate state, always call `log_event` and `restart_tunnel_service()` (for CF projects)
- When adding new web API routes that mutate state, always call `log_event`
- The `devtunnel set` command and `PATCH /api/projects/<name>` handle auto-restart of both app services and the tunnel
- MCP tools should delegate to the CLI (`cli(["command", ...args])`) rather than manipulating state directly
- App service systemd templates must include `StartLimitIntervalSec` and `StartLimitBurst` in the `[Unit]` section
- cloudflared does NOT support SIGHUP — config changes require a full service restart
