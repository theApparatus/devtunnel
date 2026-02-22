#!/usr/bin/env bash
set -euo pipefail

#
# devtunnel installer
#
# Usage: ./install.sh
#
# Interactive setup that walks you through the full configuration.
#

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${GREEN}>>>${NC} $*"; }
warn() { echo -e "${YELLOW}>>>${NC} $*"; }
die() { echo -e "${RED}error:${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║        devtunnel installer            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# Check dependencies
for cmd in curl jq; do
    if ! command -v "$cmd" &>/dev/null; then
        die "$cmd is required. Install it first (sudo apt install $cmd)"
    fi
done

# Step 1: Install cloudflared
if command -v cloudflared &>/dev/null; then
    info "cloudflared already installed: $(cloudflared --version 2>&1 | head -1)"
else
    info "Installing cloudflared..."
    curl -sSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
    sudo dpkg -i /tmp/cloudflared.deb
    info "cloudflared installed: $(cloudflared --version 2>&1 | head -1)"
fi

# Step 2: Authenticate
if [[ -f "$HOME/.cloudflared/cert.pem" ]]; then
    info "cloudflared already authenticated (cert.pem exists)"
else
    info "Authenticating with Cloudflare..."
    echo "A browser window will open. Log in and select your domain."
    echo ""
    cloudflared login
fi

# Step 3: Get domain
echo ""
read -rp "Enter your Cloudflare domain (e.g., example.com): " DOMAIN
[[ -n "$DOMAIN" ]] || die "Domain is required"

# Step 4: Create tunnel
TUNNEL_NAME="devbox"
echo ""
info "Creating tunnel '${TUNNEL_NAME}'..."

TUNNEL_OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1) || {
    if echo "$TUNNEL_OUTPUT" | grep -q "already exists"; then
        warn "Tunnel '${TUNNEL_NAME}' already exists"
        TUNNEL_ID=$(cloudflared tunnel list -o json 2>/dev/null | jq -r ".[] | select(.name==\"$TUNNEL_NAME\") | .id")
    else
        die "Failed to create tunnel: $TUNNEL_OUTPUT"
    fi
}

if [[ -z "${TUNNEL_ID:-}" ]]; then
    TUNNEL_ID=$(echo "$TUNNEL_OUTPUT" | grep -oP 'with id \K[a-f0-9-]+')
fi

[[ -n "$TUNNEL_ID" ]] || die "Could not determine tunnel ID"
info "Tunnel ID: ${TUNNEL_ID}"

# Step 5: Wildcard DNS
info "Creating wildcard DNS record..."
cloudflared tunnel route dns "$TUNNEL_NAME" "*.${DOMAIN}" 2>&1 || warn "DNS record may already exist"

# Step 5b: Tailscale detection (optional)
echo ""
echo -e "${CYAN}── Tailscale ──${NC}"
echo ""
if command -v tailscale &>/dev/null; then
    TS_VERSION=$(tailscale version 2>/dev/null | head -1)
    info "Tailscale installed: ${TS_VERSION}"
    TS_STATUS=$(tailscale status --json 2>/dev/null || true)
    if [[ -n "$TS_STATUS" ]]; then
        TS_BACKEND=$(echo "$TS_STATUS" | jq -r '.BackendState // empty')
        if [[ "$TS_BACKEND" == "Running" ]]; then
            TS_HOSTNAME=$(echo "$TS_STATUS" | jq -r '.Self.DNSName // empty' | sed 's/\.$//')
            info "Tailscale connected: ${GREEN}${TS_HOSTNAME}${NC}"

            echo ""
            read -rp "Enable Tailscale Funnel (public access via *.ts.net)? [y/N]: " ENABLE_FUNNEL
            if [[ "${ENABLE_FUNNEL,,}" == "y" ]]; then
                echo ""
                info "Funnel must be enabled in your tailnet admin console."
                echo "  Open: https://login.tailscale.com/admin/dns"
                echo "  Enable HTTPS and Funnel for this device."
                echo ""
            fi
        else
            warn "Tailscale is installed but not connected."
            echo "  Connect with: sudo tailscale up"
        fi
    else
        warn "Tailscale is installed but not connected."
        echo "  Connect with: sudo tailscale up"
    fi
else
    info "Tailscale not installed (optional — for private tailnet tunnels)."
    echo "  Install from: https://tailscale.com/download"
fi
echo ""

# Step 6: Install the script + web UI
mkdir -p "$HOME/.local/bin"
cp "${SCRIPT_DIR}/bin/devtunnel" "$HOME/.local/bin/devtunnel"
chmod +x "$HOME/.local/bin/devtunnel"

# Patch the script with user's values
sed -i "s|^DOMAIN=.*|DOMAIN=\"${DOMAIN}\"|" "$HOME/.local/bin/devtunnel"
sed -i "s|^TUNNEL_ID=.*|TUNNEL_ID=\"${TUNNEL_ID}\"|" "$HOME/.local/bin/devtunnel"

# Install web UI
mkdir -p "$HOME/.local/share/devtunnel/web/templates" "$HOME/.local/share/devtunnel/web/static"
cp "${SCRIPT_DIR}/web/app.py" "$HOME/.local/share/devtunnel/web/"
cp "${SCRIPT_DIR}/web/templates/index.html" "$HOME/.local/share/devtunnel/web/templates/"

# Check for Flask
if ! python3 -c "import flask" 2>/dev/null; then
    info "Installing Flask for web UI..."
    pip3 install --break-system-packages flask 2>/dev/null || pip3 install flask 2>/dev/null || warn "Could not install Flask. Web UI won't work."
fi

# Ensure PATH
if ! echo "$PATH" | tr ':' '\n' | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    export PATH="$HOME/.local/bin:$PATH"
    info "Added ~/.local/bin to PATH"
fi

# Step 7: Initialize state and config
echo '{"projects":{}}' > "$HOME/.cloudflared/devtunnel.json"

CREDS_FILE="$HOME/.cloudflared/${TUNNEL_ID}.json"
cat > "$HOME/.cloudflared/config.yml" <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS_FILE}

ingress:
  - service: http_status:404
EOF

info "Tunnel config written to ~/.cloudflared/config.yml"

# Step 8: Systemd services
mkdir -p "$HOME/.config/systemd/user"
cp "${SCRIPT_DIR}/systemd/cloudflared.service" "$HOME/.config/systemd/user/"
cp "${SCRIPT_DIR}/systemd/devtunnel-web.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now cloudflared.service
systemctl --user enable --now devtunnel-web.service

# Enable linger
sudo loginctl enable-linger "$USER" 2>/dev/null || warn "Could not enable linger (tunnel won't survive logout without it)"

sleep 2
if systemctl --user is-active cloudflared.service &>/dev/null; then
    info "Tunnel service is ${GREEN}running${NC}"
else
    warn "Tunnel service failed to start. Check: journalctl --user -u cloudflared.service"
fi

# Step 9: Optional API setup
echo ""
echo -e "${CYAN}── Optional: Locked project support ──${NC}"
echo ""
echo "To lock projects behind email OTP, you need:"
echo "  1. A Cloudflare API token (Access: Apps and Policies Edit)"
echo "  2. Your Cloudflare Account ID"
echo ""
read -rp "Set up locked project support now? [y/N]: " SETUP_API

if [[ "${SETUP_API,,}" == "y" ]]; then
    echo ""
    read -rp "Cloudflare Account ID: " CF_ACCOUNT_ID
    read -rp "Cloudflare API Token: " CF_API_TOKEN

    if [[ -n "$CF_ACCOUNT_ID" && -n "$CF_API_TOKEN" ]]; then
        # Add to bashrc
        {
            echo ""
            echo "# devtunnel - Cloudflare Zero Trust"
            echo "export CLOUDFLARE_ACCOUNT_ID=\"${CF_ACCOUNT_ID}\""
            echo "export CLOUDFLARE_API_TOKEN=\"${CF_API_TOKEN}\""
        } >> "$HOME/.bashrc"

        export CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT_ID"
        export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN"

        info "Credentials saved to ~/.bashrc"

        # Enable OTP
        info "Enabling One-Time PIN identity provider..."
        devtunnel setup-otp
    fi
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          Setup complete!              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════╝${NC}"
echo ""
echo "  Try it:"
echo ""
echo "    devtunnel add myapp 3000 --public"
echo "    devtunnel ls"
echo "    devtunnel help"
echo ""
echo "  Web UI: http://127.0.0.1:7000"
echo ""
