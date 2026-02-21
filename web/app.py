#!/usr/bin/env python3
"""
devtunnel web UI — local management interface for cloudflare tunnel projects.

Reads/writes the same state file as the devtunnel CLI (~/.cloudflared/devtunnel.json)
and calls the Cloudflare API for Access app management.

Usage:
    python3 app.py
    # or: devtunnel web
"""

import json
import os
import subprocess
import textwrap
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

DOMAIN = os.environ.get("DEVTUNNEL_DOMAIN", "")
TUNNEL_ID = os.environ.get("DEVTUNNEL_TUNNEL_ID", "")
CONFIG_DIR = Path.home() / ".cloudflared"
STATE_FILE = CONFIG_DIR / "devtunnel.json"
TUNNEL_CONFIG = CONFIG_DIR / "config.yml"
CF_API = "https://api.cloudflare.com/client/v4"

# Try to read domain/tunnel from the installed devtunnel script
DEVTUNNEL_BIN = Path.home() / ".local" / "bin" / "devtunnel"
if (not DOMAIN or not TUNNEL_ID) and DEVTUNNEL_BIN.exists():
    script = DEVTUNNEL_BIN.read_text()
    for line in script.splitlines():
        if line.startswith("DOMAIN=") and not DOMAIN:
            DOMAIN = line.split("=", 1)[1].strip().strip('"')
        if line.startswith("TUNNEL_ID=") and not TUNNEL_ID:
            TUNNEL_ID = line.split("=", 1)[1].strip().strip('"')


def get_cf_token():
    return os.environ.get("CLOUDFLARE_API_TOKEN", "")


def get_cf_account():
    return os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")


# ── State helpers ───────────────────────────────────────────────────────────

def read_state():
    if not STATE_FILE.exists():
        return {"projects": {}}
    with open(STATE_FILE) as f:
        return json.load(f)


def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Cloudflare API helpers ──────────────────────────────────────────────────

def cf_api(method, path, data=None):
    import urllib.request
    import urllib.error

    url = f"{CF_API}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {get_cf_token()}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"success": False, "errors": [{"message": body}]}


def create_access_app(name, emails):
    hostname = f"{name}.{DOMAIN}"
    account = get_cf_account()

    app_result = cf_api("POST", f"/accounts/{account}/access/apps", {
        "name": f"devbox-{name}",
        "domain": hostname,
        "type": "self_hosted",
        "session_duration": "24h",
        "self_hosted_domains": [hostname],
        "app_launcher_visible": True,
        "skip_interstitial": True,
    })

    app_id = (app_result.get("result") or {}).get("id")
    if not app_id:
        err = (app_result.get("errors") or [{}])[0].get("message", "unknown error")
        return None, f"Failed to create Access app: {err}"

    include_rules = [{"email": {"email": e.strip()}} for e in emails.split(",") if e.strip()]

    cf_api("POST", f"/accounts/{account}/access/apps/{app_id}/policies", {
        "name": "Allow invited emails",
        "decision": "allow",
        "precedence": 1,
        "include": include_rules,
        "require": [],
        "exclude": [],
    })

    return app_id, None


def delete_access_app(app_id):
    account = get_cf_account()
    cf_api("DELETE", f"/accounts/{account}/access/apps/{app_id}")


def get_access_app_policies(app_id):
    account = get_cf_account()
    result = cf_api("GET", f"/accounts/{account}/access/apps/{app_id}/policies")
    if result.get("success"):
        return result.get("result", [])
    return []


def update_access_policy_emails(app_id, policy_id, emails):
    account = get_cf_account()
    include_rules = [{"email": {"email": e.strip()}} for e in emails.split(",") if e.strip()]
    result = cf_api("PUT", f"/accounts/{account}/access/apps/{app_id}/policies/{policy_id}", {
        "name": "Allow invited emails",
        "decision": "allow",
        "precedence": 1,
        "include": include_rules,
        "require": [],
        "exclude": [],
    })
    return result.get("success", False)


# ── Config rebuild ──────────────────────────────────────────────────────────

def rebuild_config():
    state = read_state()
    lines = [
        f"tunnel: {TUNNEL_ID}",
        f"credentials-file: {CONFIG_DIR / (TUNNEL_ID + '.json')}",
        "",
        "ingress:",
    ]
    for name, proj in state.get("projects", {}).items():
        lines.append(f'  - hostname: "{name}.{DOMAIN}"')
        lines.append(f"    service: http://localhost:{proj['port']}")
    lines.append("  - service: http_status:404")
    lines.append("")

    TUNNEL_CONFIG.write_text("\n".join(lines))


def restart_tunnel():
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "cloudflared.service"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            subprocess.run(["systemctl", "--user", "restart", "cloudflared.service"],
                           capture_output=True)
            return "restarted (user service)"

        result = subprocess.run(
            ["systemctl", "is-active", "cloudflared.service"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return "running (system service) — restart manually with: sudo systemctl restart cloudflared"

        return "not running — start with: cloudflared tunnel run devbox"
    except Exception as e:
        return f"error: {e}"


def get_tunnel_status():
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "cloudflared.service"],
            capture_output=True, text=True
        )
        if result.stdout.strip() == "active":
            return "running"
        result = subprocess.run(
            ["systemctl", "is-active", "cloudflared.service"],
            capture_output=True, text=True
        )
        if result.stdout.strip() == "active":
            return "running"
        return "stopped"
    except Exception:
        return "unknown"


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", domain=DOMAIN)


@app.route("/api/status")
def api_status():
    return jsonify({
        "tunnel": get_tunnel_status(),
        "domain": DOMAIN,
        "tunnel_id": TUNNEL_ID,
        "has_api_token": bool(get_cf_token()),
        "has_account_id": bool(get_cf_account()),
    })


@app.route("/api/projects")
def api_projects():
    state = read_state()
    projects = []
    for name, proj in state.get("projects", {}).items():
        projects.append({
            "name": name,
            "port": proj.get("port"),
            "access": proj.get("access", "public"),
            "emails": proj.get("emails", ""),
            "access_app_id": proj.get("access_app_id", ""),
            "url": f"https://{name}.{DOMAIN}",
        })
    return jsonify(projects)


@app.route("/api/projects", methods=["POST"])
def api_add_project():
    data = request.json or {}
    name = data.get("name", "").strip().lower()
    port = data.get("port")
    access = data.get("access", "public")
    emails = data.get("emails", "").strip()

    # Validate
    if not name:
        return jsonify({"error": "Name is required"}), 400
    import re
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        return jsonify({"error": "Name must be a valid subdomain (lowercase alphanumeric and hyphens)"}), 400

    try:
        port = int(port)
        if port < 1 or port > 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Port must be 1-65535"}), 400

    state = read_state()
    if name in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' already exists"}), 409

    access_app_id = ""
    if access == "locked":
        if not emails:
            return jsonify({"error": "Locked access requires at least one email"}), 400
        if not get_cf_token() or not get_cf_account():
            return jsonify({"error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be set for locked projects"}), 400
        access_app_id, err = create_access_app(name, emails)
        if err:
            return jsonify({"error": err}), 500

    state.setdefault("projects", {})[name] = {
        "port": port,
        "access": access,
        "emails": emails,
        "access_app_id": access_app_id or "",
    }
    write_state(state)
    rebuild_config()
    tunnel_msg = restart_tunnel()

    return jsonify({
        "ok": True,
        "url": f"https://{name}.{DOMAIN}",
        "tunnel": tunnel_msg,
    })


@app.route("/api/projects/<name>", methods=["DELETE"])
def api_remove_project(name):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    proj = state["projects"][name]
    app_id = proj.get("access_app_id")
    if app_id:
        if not get_cf_token() or not get_cf_account():
            return jsonify({"error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID needed to delete Access app"}), 400
        delete_access_app(app_id)

    del state["projects"][name]
    write_state(state)
    rebuild_config()
    tunnel_msg = restart_tunnel()

    return jsonify({"ok": True, "tunnel": tunnel_msg})


@app.route("/api/projects/<name>/emails", methods=["PUT"])
def api_update_emails(name):
    data = request.json or {}
    emails = data.get("emails", "").strip()

    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    proj = state["projects"][name]
    if proj.get("access") != "locked":
        return jsonify({"error": "Project is not locked — change access type first"}), 400

    app_id = proj.get("access_app_id")
    if not app_id:
        return jsonify({"error": "No Access app found for this project"}), 400

    if not get_cf_token() or not get_cf_account():
        return jsonify({"error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID required"}), 400

    # Get existing policies to find the one to update
    policies = get_access_app_policies(app_id)
    if policies:
        policy_id = policies[0].get("id")
        if policy_id:
            update_access_policy_emails(app_id, policy_id, emails)

    proj["emails"] = emails
    write_state(state)

    return jsonify({"ok": True})


@app.route("/api/tunnel/restart", methods=["POST"])
def api_restart_tunnel():
    rebuild_config()
    msg = restart_tunnel()
    return jsonify({"ok": True, "message": msg})


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  devtunnel web UI")
    print(f"  Domain: {DOMAIN}")
    print(f"  State:  {STATE_FILE}")
    print(f"  Config: {TUNNEL_CONFIG}\n")
    app.run(host="127.0.0.1", port=7000, debug=False)
