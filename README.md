# devtunnel

Expose local dev projects to the internet via Cloudflare Tunnel with optional access control. One command to share a project publicly or lock it down to specific people via email OTP.

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
                                │ Access Policy │
                                │  --public     │ → anyone
                                │  --emails     │ → OTP login
                                └──────────────┘
```

A single persistent Cloudflare Tunnel runs on your dev box. A wildcard DNS record (`*.yourdomain.com`) routes all subdomains to the tunnel. The `devtunnel` CLI manages per-project ingress rules and optionally creates Cloudflare Zero Trust Access applications to lock down individual subdomains.

### Public vs Locked

**Public** (`--public`): No authentication. Anyone with the URL can access it. Good for portfolios, demos, public APIs.

**Locked** (`--emails`): Cloudflare Access sits in front of the subdomain. When someone visits:

1. Cloudflare intercepts the request at the edge (before it reaches your box)
2. Visitor sees a login screen asking for their email
3. If the email is in the allowed list → one-time PIN sent to their inbox
4. They enter the PIN → 24h session cookie → access granted
5. If the email is NOT in the list → denied, no email sent

Your dev server never sees unauthorized traffic.

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

### 9. (Optional) Set up for locked projects

For locked projects (email OTP), you need a Cloudflare API token:

1. Go to https://dash.cloudflare.com/profile/api-tokens
2. Create a token with **Access: Apps and Policies Edit** permission
3. Find your Account ID in the Cloudflare dashboard URL

```bash
# Add to ~/.bashrc
export CLOUDFLARE_ACCOUNT_ID="your-account-id"
export CLOUDFLARE_API_TOKEN="your-api-token"
```

Then enable the One-Time PIN identity provider (one-time setup):

```bash
devtunnel setup-otp
```

## Usage

```bash
# Public project — no auth, anyone can access
devtunnel add portfolio 3000 --public

# Locked project — only these emails get in via OTP
devtunnel add client-app 5173 --emails client@company.com,pm@company.com

# Multiple emails
devtunnel add prototype 8080 --emails alice@gmail.com,bob@work.com,carol@dev.io

# List all projects
devtunnel ls

# Remove a project (also deletes the Access app if locked)
devtunnel rm client-app

# Restart the tunnel (after manual config changes)
devtunnel restart

# Check tunnel status
devtunnel status
```

### Example workflow

```bash
# Start working on a client project
cd ~/projects/acme-dashboard
npm run dev -- --port 5173

# Share with the client
devtunnel add acme 5173 --emails client@acme.com

# Send them the link: https://acme.fixshifted.com
# They get an email OTP login, 24h session

# Done for the day? Leave it up or tear it down
devtunnel rm acme
```

## Commands

| Command | Description |
|---|---|
| `devtunnel add <name> <port> --public` | Add a public project |
| `devtunnel add <name> <port> --emails a@b,c@d` | Add a locked project |
| `devtunnel rm <name>` | Remove a project and its Access app |
| `devtunnel ls` | List all active projects |
| `devtunnel restart` | Restart the tunnel service |
| `devtunnel status` | Show tunnel and project status |
| `devtunnel setup-otp` | Enable email OTP (one-time setup) |
| `devtunnel help` | Show help |

## File Locations

| File | Purpose |
|---|---|
| `~/.local/bin/devtunnel` | The CLI script |
| `~/.cloudflared/config.yml` | Cloudflare Tunnel ingress config (auto-managed) |
| `~/.cloudflared/devtunnel.json` | Project state (ports, access type, Access app IDs) |
| `~/.cloudflared/cert.pem` | Tunnel auth certificate |
| `~/.cloudflared/<tunnel-id>.json` | Tunnel credentials |
| `~/.config/systemd/user/cloudflared.service` | Systemd user service |

## Architecture

```
devtunnel add myapp 3000 --emails user@co.com
    │
    ├─ Updates ~/.cloudflared/devtunnel.json (state)
    ├─ Rebuilds ~/.cloudflared/config.yml (ingress rules)
    ├─ Creates Cloudflare Access Application via API
    ├─ Creates Access Policy (allow listed emails)
    └─ Restarts cloudflared systemd service
```

The tunnel runs as a **systemd user service** with linger enabled, so it survives reboots and doesn't need root. The wildcard DNS record means you never need to touch DNS — just add a project and the subdomain works immediately.

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

**Access app not created (API errors):**
Check that `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` are set and the token has Access:Edit permissions.

**Visitor sees 404:**
Make sure your dev server is actually running on the port you specified.

**OTP email not arriving:**
The email address must exactly match what's in the `--emails` list. Check spam folders. Run `devtunnel setup-otp` if you haven't already.

## License

MIT
