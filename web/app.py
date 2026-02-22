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
import re
import subprocess
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
SERVICE_PREFIX = "devtunnel-app"

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


def get_access_logs(limit=50):
    account = get_cf_account()
    result = cf_api("GET", f"/accounts/{account}/access/logs/access_requests?limit={limit}&direction=desc")
    if result.get("success"):
        return result.get("result", [])
    return []


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
        if proj.get("provider", "cloudflare") == "tailscale":
            continue
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


# ── Tailscale helpers ──────────────────────────────────────────────────────

TS_FUNNEL_PORTS = [443, 8443, 10000]


def detect_tailscale():
    """Returns tailscale hostname or None."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("BackendState") != "Running":
            return None
        hostname = data.get("Self", {}).get("DNSName", "")
        return hostname.rstrip(".") if hostname else None
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def ts_allocate_port(state, mode):
    """Allocate an external HTTPS port for Tailscale serve/funnel."""
    used_ports = set()
    for proj in state.get("projects", {}).values():
        if proj.get("provider") == "tailscale" and proj.get("ts_port"):
            used_ports.add(int(proj["ts_port"]))

    if mode == "funnel":
        for p in TS_FUNNEL_PORTS:
            if p not in used_ports:
                return p
        return None  # all funnel ports in use
    else:
        # Serve: try funnel ports first, then 4443, 5443, 6443, ...
        for p in TS_FUNNEL_PORTS:
            if p not in used_ports:
                return p
        base = 4443
        while base <= 65535:
            if base not in used_ports:
                return base
            base += 1000
        return None


def ts_add(local_port, ext_port, mode):
    """Run tailscale serve/funnel to set up the tunnel."""
    try:
        result = subprocess.run(
            ["tailscale", mode, f"--https={ext_port}", "--bg", f"localhost:{local_port}"],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0, result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def ts_remove(ext_port, mode):
    """Remove a tailscale serve/funnel."""
    try:
        subprocess.run(
            ["tailscale", mode, f"--https={ext_port}", "off"],
            capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def ts_url(hostname, ext_port):
    """Build the public/tailnet URL for a Tailscale project."""
    ext_port = int(ext_port)
    if ext_port == 443:
        return f"https://{hostname}"
    return f"https://{hostname}:{ext_port}"


# ── App service management ─────────────────────────────────────────────────

SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"


def service_name(name):
    return f"{SERVICE_PREFIX}-{name}.service"


def get_app_service_status(name):
    svc = service_name(name)
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", svc],
            capture_output=True, text=True
        )
        if result.stdout.strip() == "active":
            return "running"
        result2 = subprocess.run(
            ["systemctl", "--user", "is-enabled", svc],
            capture_output=True, text=True
        )
        if result2.returncode == 0:
            return "stopped"
        return "none"
    except Exception:
        return "none"


def create_app_service(name, directory, cmd, port):
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    svc_file = SYSTEMD_USER_DIR / service_name(name)
    svc_file.write_text(f"""[Unit]
Description=devtunnel app: {name} (port {port})
After=network-online.target

[Service]
Type=simple
WorkingDirectory={directory}
ExecStart=/bin/bash -c '{cmd}'
Restart=always
RestartSec=3
Environment=PORT={port}
Environment=NODE_ENV=development

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", service_name(name)],
        capture_output=True
    )


def remove_app_service(name):
    svc = service_name(name)
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", svc],
        capture_output=True
    )
    svc_file = SYSTEMD_USER_DIR / svc
    if svc_file.exists():
        svc_file.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def control_app_service(name, action):
    svc = service_name(name)
    result = subprocess.run(
        ["systemctl", "--user", action, svc],
        capture_output=True, text=True
    )
    return result.returncode == 0


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", domain=DOMAIN)


@app.route("/api/status")
def api_status():
    state = read_state()
    projects = state.get("projects", {})
    project_count = len(projects)
    locked_count = sum(1 for p in projects.values() if p.get("access") == "locked")
    ts_count = sum(1 for p in projects.values() if p.get("provider") == "tailscale")
    ts_hostname = detect_tailscale()
    return jsonify({
        "tunnel": get_tunnel_status(),
        "domain": DOMAIN,
        "tunnel_id": TUNNEL_ID,
        "has_api_token": bool(get_cf_token()),
        "has_account_id": bool(get_cf_account()),
        "project_count": project_count,
        "locked_count": locked_count,
        "tailscale_available": ts_hostname is not None,
        "tailscale_hostname": ts_hostname or "",
        "ts_project_count": ts_count,
    })


@app.route("/api/projects")
def api_projects():
    state = read_state()
    ts_hostname = detect_tailscale()
    projects = []
    for name, proj in state.get("projects", {}).items():
        provider = proj.get("provider", "cloudflare")
        if provider == "tailscale" and ts_hostname and proj.get("ts_port"):
            url = ts_url(ts_hostname, proj["ts_port"])
        else:
            url = f"https://{name}.{DOMAIN}"
        projects.append({
            "name": name,
            "port": proj.get("port"),
            "provider": provider,
            "access": proj.get("access", "public"),
            "emails": proj.get("emails", ""),
            "access_app_id": proj.get("access_app_id", ""),
            "ts_port": proj.get("ts_port", ""),
            "url": url,
            "dir": proj.get("dir", ""),
            "cmd": proj.get("cmd", ""),
            "service": get_app_service_status(name),
        })
    return jsonify(projects)


@app.route("/api/projects", methods=["POST"])
def api_add_project():
    data = request.json or {}
    name = data.get("name", "").strip().lower()
    port = data.get("port")
    provider = data.get("provider", "cloudflare")
    access = data.get("access", "public")
    ts_mode = data.get("ts_mode", "serve")  # serve or funnel
    emails = data.get("emails", "").strip()
    directory = data.get("dir", "").strip()
    cmd = data.get("cmd", "").strip()

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name):
        return jsonify({"error": "Name must be a valid subdomain (lowercase alphanumeric and hyphens)"}), 400

    try:
        port = int(port)
        if port < 1 or port > 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Port must be 1-65535"}), 400

    if directory and not Path(directory).is_dir():
        return jsonify({"error": f"Directory does not exist: {directory}"}), 400

    state = read_state()
    if name in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' already exists"}), 409

    if provider == "tailscale":
        # Tailscale path
        ts_hostname = detect_tailscale()
        if not ts_hostname:
            return jsonify({"error": "Tailscale is not available. Install and connect: sudo tailscale up"}), 400

        ts_port = ts_allocate_port(state, ts_mode)
        if ts_port is None:
            if ts_mode == "funnel":
                return jsonify({"error": "All Tailscale Funnel ports (443, 8443, 10000) are in use. Remove a Funnel project first."}), 400
            return jsonify({"error": "No available Tailscale ports"}), 400

        ok, err_msg = ts_add(port, ts_port, ts_mode)
        if not ok:
            if ts_mode == "funnel":
                return jsonify({"error": f"Tailscale Funnel failed. Enable it in your tailnet admin console. {err_msg}"}), 500
            return jsonify({"error": f"Tailscale Serve failed: {err_msg}"}), 500

        state.setdefault("projects", {})[name] = {
            "port": port,
            "provider": "tailscale",
            "access": ts_mode,
            "ts_port": ts_port,
            "dir": directory,
            "cmd": cmd,
        }
        write_state(state)

        if directory and cmd:
            create_app_service(name, directory, cmd, port)

        url = ts_url(ts_hostname, ts_port)
        return jsonify({"ok": True, "url": url})

    else:
        # Cloudflare path
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
            "provider": "cloudflare",
            "access": access,
            "emails": emails,
            "access_app_id": access_app_id or "",
            "dir": directory,
            "cmd": cmd,
        }
        write_state(state)
        rebuild_config()
        tunnel_msg = restart_tunnel()

        if directory and cmd:
            create_app_service(name, directory, cmd, port)

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
    provider = proj.get("provider", "cloudflare")

    if provider == "tailscale":
        ts_port = proj.get("ts_port")
        ts_access = proj.get("access", "serve")
        if ts_port:
            ts_remove(ts_port, ts_access)
    else:
        app_id = proj.get("access_app_id")
        if app_id:
            if not get_cf_token() or not get_cf_account():
                return jsonify({"error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID needed to delete Access app"}), 400
            delete_access_app(app_id)

    # Remove app service if it exists
    remove_app_service(name)

    del state["projects"][name]
    write_state(state)

    tunnel_msg = ""
    if provider != "tailscale":
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
    if proj.get("provider", "cloudflare") == "tailscale":
        return jsonify({"error": "Email access control is not supported with Tailscale"}), 400
    if proj.get("access") != "locked":
        return jsonify({"error": "Project is not locked — change access type first"}), 400

    app_id = proj.get("access_app_id")
    if not app_id:
        return jsonify({"error": "No Access app found for this project"}), 400

    if not get_cf_token() or not get_cf_account():
        return jsonify({"error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID required"}), 400

    policies = get_access_app_policies(app_id)
    if policies:
        policy_id = policies[0].get("id")
        if policy_id:
            update_access_policy_emails(app_id, policy_id, emails)

    proj["emails"] = emails
    write_state(state)

    return jsonify({"ok": True})


@app.route("/api/projects/<name>/service/<action>", methods=["POST"])
def api_service_action(name, action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action. Use start, stop, or restart."}), 400

    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    proj = state["projects"][name]

    # If no service exists but dir+cmd are in state, create it on first start
    if action == "start" and get_app_service_status(name) == "none":
        directory = proj.get("dir", "")
        cmd = proj.get("cmd", "")
        if directory and cmd:
            create_app_service(name, directory, cmd, proj.get("port", 0))
            return jsonify({"ok": True, "status": get_app_service_status(name)})
        return jsonify({"error": "No service configured. Add --dir and --cmd to the project."}), 400

    ok = control_app_service(name, action)
    return jsonify({"ok": ok, "status": get_app_service_status(name)})


@app.route("/api/tunnel/restart", methods=["POST"])
def api_restart_tunnel():
    rebuild_config()
    msg = restart_tunnel()
    return jsonify({"ok": True, "message": msg})


@app.route("/api/logs")
def api_logs():
    if not get_cf_token() or not get_cf_account():
        return jsonify({"error": "API credentials not configured"}), 400

    limit = request.args.get("limit", 50, type=int)
    logs = get_access_logs(limit=min(limit, 100))

    # Map to our domain's projects
    state = read_state()
    project_domains = {f"{n}.{DOMAIN}": n for n in state.get("projects", {})}

    entries = []
    for log in logs:
        domain = log.get("app_domain", "")
        project_name = project_domains.get(domain, "")
        entries.append({
            "email": log.get("user_email", ""),
            "action": log.get("action", ""),
            "allowed": log.get("allowed", False),
            "domain": domain,
            "project": project_name,
            "ip": log.get("ip_address", ""),
            "connection": log.get("connection", ""),
            "created_at": log.get("created_at", ""),
            "ray_id": log.get("ray_id", ""),
        })

    return jsonify(entries)


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  devtunnel web UI")
    print(f"  Domain: {DOMAIN}")
    print(f"  State:  {STATE_FILE}")
    print(f"  Config: {TUNNEL_CONFIG}\n")
    app.run(host="0.0.0.0", port=7000, debug=False)
