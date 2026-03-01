# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

devtunnel is a CLI + web UI tool for exposing local dev projects via Cloudflare Tunnel or Tailscale. It manages tunnel routes, auth gate with invite codes, systemd app services, structured logging, and health checks.

## Project Structure

```
bin/devtunnel                          Bash CLI (single file, ~1200 lines)
web/app.py                             Flask web UI backend (~850 lines)
web/templates/index.html               Single-page web UI (vanilla JS, ~1100 lines)
gate/gate.py                           Auth gate proxy (~600 lines)
fly/                                   FRP server deployment on Fly.io (Dockerfile, fly.toml, frps.toml)
systemd/cloudflared.service            Cloudflare tunnel systemd unit
systemd/devtunnel-web.service          Web UI systemd unit
systemd/devtunnel-healthcheck.service  Health check runner (oneshot)
systemd/devtunnel-healthcheck.timer    60s health check timer
install.sh                             Interactive installer
```

### FRP Server (`fly/`)

FRP server deployment config for Fly.io (`frps-fixshifted` app). The `frps` binary handles public routing via `*.app.fixshifted.com`. Deployed with `fly deploy` from the `fly/` directory.

### MCP Plugin (out-of-tree)

The MCP plugin lives at `~/.claude/plugins/cache/devtunnel-local/devtunnel/1.0.0/server/src/index.ts`. It exposes devtunnel functionality as MCP tools. Most tools delegate to the CLI via `cli()` helper; keep it that way so logging and auto-restart behavior stay consistent.

### Auth Gate (`gate/gate.py`)

Cookie-based auth proxy for locked projects. Deployed to `~/.local/share/devtunnel/gate/gate.py`. Runs on port 7500 as a systemd service (`devtunnel-gate`). Sits between FRP (on Fly.io) and apps:

```
visitor → *.app.fixshifted.com → Fly.io (frps) → gate.py:7500 → app:PORT
```

gate.py reads from the shared state file and enforces auth via:
- Password login (`auth_user` / `auth_pass` per project)
- Invite codes (`invite_codes` array, supports `?invite=CODE` URL param)
- Cookie sessions (signed with `cookie_secret`, 30-day expiry)

## Key State Files (runtime, not in repo)

- `~/.config/devtunnel/state.json` — **single shared state file** for CLI, web app, gate.py, and MCP plugin. Contains projects (ports, access, auth credentials, invite codes, dir, cmd) and `cookie_secret`.
- `~/.cloudflared/config.yml` — generated cloudflared ingress config (for DNS routing only)
- `~/.local/share/devtunnel/logs/devtunnel.log` — JSONL operations log (5MB rotation)
- `~/.local/share/devtunnel/healthcheck-state.json` — per-project failure counters
- `~/.config/devtunnel/auth_events.db` — SQLite auth event log (managed by gate.py)

## Architecture Patterns

### State File (`~/.config/devtunnel/state.json`)

All components share this single file. Structure:
```json
{
  "projects": {
    "myapp": {
      "port": 3000,
      "access": "locked",
      "provider": "cloudflare",
      "auth_user": "myapp",
      "auth_pass": "salt:sha256hash",
      "invite_codes": [
        {"code": "ABC123", "email": "user@example.com", "expires_at": null, "max_uses": 5, "uses": 0}
      ],
      "dir": "/home/dev/projects/myapp",
      "cmd": "npm run dev"
    }
  },
  "cookie_secret": "hex-string"
}
```

- `auth_user`/`auth_pass` — only for locked projects, generated on `devtunnel add --locked`
- `invite_codes` — array of invite code objects, managed by `devtunnel codes` CLI or web API
- `cookie_secret` — top-level, auto-generated on first init, used by gate.py for session signing
- Public projects omit `auth_user`, `auth_pass`, and `invite_codes`

### Invite Codes Schema

Each code: `{code, email, expires_at, max_uses, uses}`
- `email` — null for anonymous codes (gate.py ignores this, tracked for admin reference)
- `max_uses` — null means unlimited
- `expires_at` — null means never expires, ISO 8601 string otherwise
- `uses` — incremented by gate.py on each successful use
- CLI subcommands: `devtunnel codes <project> [add|edit|rm]`
- Web API: GET/POST on `/api/projects/<name>/codes`, PATCH/DELETE on `/api/projects/<name>/codes/<code>`

### CLI (`bin/devtunnel`)
- Bash script, `set -euo pipefail`
- State managed via `jq` atomic read-modify-write (`jq ... > tmp && mv tmp state`)
- Helpers: `die()`, `info()`, `warn()`, `log_event()`, `restart_tunnel_service()`
- App services are systemd user units created from a template in `create_app_service()`
- `log_event level action [project] [msg]` writes JSONL to the shared log file
- `restart_tunnel_service()` is the single place that restarts cloudflared — all mutation commands call it
- `init_state()` ensures state dir exists, creates state file with `cookie_secret` if missing

### Web UI (`web/app.py`)
- Flask, no external dependencies beyond Flask
- Same state file as CLI — reads/writes `~/.config/devtunnel/state.json`
- `log_event(level, action, project, msg)` — Python equivalent of CLI logging
- API endpoints mirror CLI commands: POST add, DELETE remove, PATCH update, service control, codes CRUD
- Template is a single HTML file with inline CSS and vanilla JS (no build step)
- Binds to `0.0.0.0:7000` (accessible via Tailscale tailnet)

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
- Cloudflare is only used for DNS tunneling — no CF Access API calls
- All jq operations on the state file must preserve top-level keys like `cookie_secret` (use targeted paths, not full rewrites)
- gate.py is the auth enforcement layer — CLI/web manage codes but gate.py validates them at request time
- Locked projects need `auth_user`, `auth_pass`, and `invite_codes` in state for gate.py to work
- README.md is stale — references `~/.cloudflared/devtunnel.json` and Cloudflare-only architecture; actual state file is `~/.config/devtunnel/state.json` and routing uses FRP on Fly.io
