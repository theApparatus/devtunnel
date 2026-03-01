# devtunnel

Expose local dev projects to the internet via Cloudflare Tunnel or Tailscale with optional access control, structured logging, and self-healing health checks. One command to share a project publicly or lock it down with access codes.

Built for devs who need to show work-in-progress to clients, friends, or teammates without deploying to a staging environment.

## How It Works

```
your dev box                    cloudflare edge                    visitor
┌──────────────┐               ┌──────────────────┐              ┌──────────┐
│ localhost:3000├──── tunnel ───┤ project.your.com │◄─── HTTPS ──┤ browser  │
│ localhost:5173├──── tunnel ───┤ client.your.com  │              │          │
│ localhost:8080├──── tunnel ───┤ api.your.com     │              │          │
└──────────────┘               └──────────────────┘              └──────────┘
                                       │
                                ┌──────┴───────┐
                                │ Access Mode   │
                                │  --public     │ → anyone
                                │  --locked     │ → access codes
                                └──────────────┘
```

A single persistent Cloudflare Tunnel runs on your dev box. A wildcard DNS record (`*.yourdomain.com`) routes all subdomains to the tunnel. The `devtunnel` CLI manages per-project ingress rules and access codes to control who can reach locked projects.

### Public vs Locked

**Public** (`--public`): No authentication. Anyone with the URL can access it. Good for portfolios, demos, public APIs. This is the default.

**Locked** (`--locked`): Projects require access codes. Auth enforcement happens externally. Create and manage codes with `devtunnel codes`:

```bash
devtunnel add client-app 5173 --locked

# Create a code for a specific user
devtunnel codes client-app add client@company.com

# Create an anonymous code with usage limits
devtunnel codes client-app add --no-email --max 10

# Create a code that expires
devtunnel codes client-app add user@test.com --expire 2026-04-01T00:00:00Z

# List, edit, delete codes
devtunnel codes client-app
devtunnel codes client-app edit abc123 --max 20 --reset-uses
devtunnel codes client-app rm abc123
```

## Prerequisites

- A domain on Cloudflare (DNS managed by Cloudflare)
- Linux (tested on Ubuntu 24.04)
- `jq` installed (`sudo apt install jq`)
- `curl` installed

## Installation

### 1. Install cloudflared

```bash
curl -sSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
```

### 2. Authenticate with Cloudflare

```bash
cloudflared login
```

This opens a browser. Log in and select your domain. A certificate is saved to `~/.cloudflared/cert.pem`.

### 3. Create a named tunnel

```bash
cloudflared tunnel create devbox
```

Note the **Tunnel ID** from the output (a UUID like `246d495b-e6a0-4bc1-8643-21e84914e8e9`).

### 4. Create wildcard DNS

```bash
cloudflared tunnel route dns devbox "*.yourdomain.com"
```

### 5. Install devtunnel

```bash
# Copy the script
cp bin/devtunnel ~/.local/bin/devtunnel
chmod +x ~/.local/bin/devtunnel

# Make sure ~/.local/bin is in your PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 6. Configure the script

Edit `~/.local/bin/devtunnel` and update these values at the top:

```bash
DOMAIN="yourdomain.com"          # Your Cloudflare domain
TUNNEL_ID="your-tunnel-uuid"     # From step 3
```

### 7. Seed the initial config

```bash
# Create state file
echo '{"projects":{}}' > ~/.cloudflared/devtunnel.json

# Create initial tunnel config
cat > ~/.cloudflared/config.yml <<EOF
tunnel: YOUR-TUNNEL-ID
credentials-file: /home/YOUR-USER/.cloudflared/YOUR-TUNNEL-ID.json

ingress:
  - service: http_status:404
EOF
```

### 8. Install the systemd service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/cloudflared.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service

# Allow service to run without being logged in
sudo loginctl enable-linger $USER
```

## Usage

```bash
# Public project — no auth, anyone can access (default)
devtunnel add portfolio 3000 --public

# Locked project — requires access codes
devtunnel add client-app 5173 --locked

# Auto-managed app process (creates a systemd service that auto-restarts on crash/reboot)
devtunnel add myapp 3000 --public --dir ~/projects/myapp --cmd "npm run dev"

# Manage access codes for locked projects
devtunnel codes client-app add client@company.com
devtunnel codes client-app add --no-email --max 5
devtunnel codes client-app

# Update a project's settings (auto-restarts service and tunnel as needed)
devtunnel set myapp --port 3001
devtunnel set myapp --cmd "npm start" --dir ~/projects/myapp-v2

# List all projects (shows service status)
devtunnel ls

# Remove a project (also stops the app service)
devtunnel rm client-app

# Restart / stop a project's app service
devtunnel restart myapp
devtunnel stop myapp

# Tail a project's logs
devtunnel logs myapp

# View operations log (structured JSONL log of all CLI and web actions)
devtunnel log
devtunnel log 20

# Restart the tunnel (after manual config changes)
devtunnel restart

# Check tunnel status
devtunnel status
```

### Example workflow

```bash
# Share a project with a client — auto-starts and auto-restarts the dev server
devtunnel add acme 5173 --locked --dir ~/projects/acme-dashboard --cmd "npm run dev"

# Create an access code for the client
devtunnel codes acme add client@acme.com

# Send them the link: https://acme.yourdomain.com
# The dev server stays running even after reboot

# Check on it
devtunnel ls
devtunnel logs acme

# Done for the day? Leave it up or tear it down
devtunnel rm acme
```

## Commands

| Command | Description |
|---|---|
| `devtunnel add <name> <port> --public` | Add a public project (default) |
| `devtunnel add <name> <port> --locked` | Add a locked project (requires access codes) |
| `devtunnel add ... --dir PATH --cmd CMD` | Also create an auto-managed app service |
| `devtunnel rm <name>` | Remove a project and its service |
| `devtunnel set <name> [--cmd C] [--dir D] [--port P]` | Update project settings (auto-restarts) |
| `devtunnel codes <project>` | List access codes |
| `devtunnel codes <project> add <email> [--max N] [--expire DT]` | Create access code |
| `devtunnel codes <project> add --no-email [--max N]` | Create anonymous code |
| `devtunnel codes <project> edit <prefix> [opts]` | Edit a code |
| `devtunnel codes <project> rm <prefix>` | Delete a code |
| `devtunnel ls` | List all active projects with service status |
| `devtunnel restart` | Restart the tunnel service |
| `devtunnel restart <name>` | Restart a project's app service |
| `devtunnel stop <name>` | Stop a project's app service |
| `devtunnel logs <name>` | Tail a project's app logs |
| `devtunnel log [N]` | Show last N operations log entries (default 50) |
| `devtunnel status` | Show tunnel and project status |
| `devtunnel web` | Start the web management UI (port 7000) |
| `devtunnel help` | Show help |

## File Locations

| File | Purpose |
|---|---|
| `~/.local/bin/devtunnel` | The CLI script |
| `~/.local/share/devtunnel/web/` | Web management UI (Flask) |
| `~/.local/share/devtunnel/logs/devtunnel.log` | Structured operations log (JSONL) |
| `~/.local/share/devtunnel/healthcheck-state.json` | Health check failure counters |
| `~/.cloudflared/config.yml` | Cloudflare Tunnel ingress config (auto-managed) |
| `~/.cloudflared/devtunnel.json` | Project state (ports, access, codes, dir, cmd) |
| `~/.cloudflared/cert.pem` | Tunnel auth certificate |
| `~/.cloudflared/<tunnel-id>.json` | Tunnel credentials |
| `~/.config/systemd/user/cloudflared.service` | Tunnel systemd service |
| `~/.config/systemd/user/devtunnel-web.service` | Web UI systemd service |
| `~/.config/systemd/user/devtunnel-healthcheck.timer` | 60s health check timer |
| `~/.config/systemd/user/devtunnel-healthcheck.service` | Health check runner (oneshot) |
| `~/.config/systemd/user/devtunnel-app-*.service` | Auto-created app services |

## Architecture

```
devtunnel add myapp 3000 --locked --dir ~/proj --cmd "npm run dev"
    │
    ├─ Updates ~/.cloudflared/devtunnel.json (state)
    ├─ Rebuilds ~/.cloudflared/config.yml (ingress rules)
    ├─ Creates systemd user service devtunnel-app-myapp.service (if --dir/--cmd)
    └─ Restarts cloudflared systemd service

devtunnel codes myapp add client@acme.com
    │
    ├─ Generates 32-char hex token
    ├─ Appends to project's codes array in state
    └─ Logs the action
```

The tunnel runs as a **systemd user service** with linger enabled, so it survives reboots and doesn't need root. The wildcard DNS record means you never need to touch DNS — just add a project and the subdomain works immediately.

## Operations Log

All CLI and web UI actions are logged to `~/.local/share/devtunnel/logs/devtunnel.log` in JSONL format:

```json
{"ts":"2026-02-24T10:30:00Z","level":"info","source":"cli","action":"add","project":"myapp","msg":"provider=cloudflare port=3000 access=public"}
```

Each entry includes: timestamp, level (info/warn/error), source (cli/web/healthcheck), action, project name, and message.

The log auto-rotates at 5MB (keeps 1 backup). View from the CLI or web UI:

```bash
devtunnel log        # last 50 entries, color-coded
devtunnel log 20     # last 20 entries
```

The web UI has an **Operations Log** tab that shows the same data with project filtering.

## Health Checks & Self-Healing

A systemd timer runs `devtunnel healthcheck` every 60 seconds:

1. Checks if `cloudflared.service` is active — restarts if not
2. For each project with a running app service, probes `http://localhost:{port}/`
3. After **2 consecutive failures** (2 minutes of unresponsiveness), automatically restarts the app service
4. Tracks failure counts in `~/.local/share/devtunnel/healthcheck-state.json`
5. All checks and restarts are logged to the operations log

Unhealthy projects show a warning indicator in the web UI.

### Systemd Start Limits

All services are hardened with start limits to prevent restart loops:
- **cloudflared / web UI**: max 10 restarts in 5 minutes
- **App services**: max 10 restarts in 60 seconds

## Web UI

The web management UI runs on port 7000 and provides:

- **Projects tab**: View all projects with status, service controls (start/stop/restart), edit button, codes button, and health indicators
- **Add Project tab**: Form to add new Cloudflare or Tailscale projects with optional app service config
- **Operations Log tab**: Structured log of all CLI and web actions with project filtering
- **Codes modal**: Create, view, and delete access codes per project
- **Edit Project modal**: Update port, working directory, or start command — auto-restarts services

## Troubleshooting

**Tunnel not starting:**
```bash
journalctl --user -u cloudflared.service -n 50
```

**"last ingress rule must match all URLs":**
The catch-all rule in `config.yml` must not have a hostname. It should be:
```yaml
ingress:
  - service: http_status:404
```

**Visitor sees 404:**
Make sure your dev server is actually running on the port you specified.

## License

MIT
