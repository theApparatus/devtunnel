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
import secrets
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session, abort

app = Flask(__name__)
app.secret_key = os.environ.get("DEVTUNNEL_SECRET_KEY", secrets.token_hex(32))


# ── CSRF Protection ────────────────────────────────────────────────────────

@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        token = request.headers.get("X-CSRF-Token") or (request.json or {}).get("_csrf_token")
        if not token or token != session.get("csrf_token"):
            abort(403, description="Invalid or missing CSRF token")


@app.route("/api/csrf-token")
def api_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return jsonify({"token": session["csrf_token"]})


# ── Config ──────────────────────────────────────────────────────────────────

DOMAIN = os.environ.get("DEVTUNNEL_DOMAIN", "")
TUNNEL_ID = os.environ.get("DEVTUNNEL_TUNNEL_ID", "")
CONFIG_DIR = Path.home() / ".cloudflared"
STATE_FILE = Path.home() / ".config" / "devtunnel" / "state.json"
TUNNEL_CONFIG = CONFIG_DIR / "config.yml"
SERVICE_PREFIX = "devtunnel-app"

# ── Structured Logging ─────────────────────────────────────────────────────

LOG_DIR = Path.home() / ".local" / "share" / "devtunnel" / "logs"
LOG_FILE = LOG_DIR / "devtunnel.log"
MAX_LOG_SIZE = 5 * 1024 * 1024


def log_event(level, action, project="", msg=""):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
        backup = LOG_FILE.with_suffix(".log.1")
        LOG_FILE.rename(backup)
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level, "source": "web", "action": action,
        "project": project, "msg": msg,
    })
    with open(LOG_FILE, "a") as f:
        f.write(entry + "\n")


# Try to read domain/tunnel from the installed devtunnel script
DEVTUNNEL_BIN = Path.home() / ".local" / "bin" / "devtunnel"
if (not DOMAIN or not TUNNEL_ID) and DEVTUNNEL_BIN.exists():
    script = DEVTUNNEL_BIN.read_text()
    for line in script.splitlines():
        if line.startswith("DOMAIN=") and not DOMAIN:
            DOMAIN = line.split("=", 1)[1].strip().strip('"')
        if line.startswith("TUNNEL_ID=") and not TUNNEL_ID:
            TUNNEL_ID = line.split("=", 1)[1].strip().strip('"')


# ── Health probe helpers ───────────────────────────────────────────────────

def probe_port(port, timeout=3):
    """Probe localhost:port and return (status_code, detail_string)."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError):
        return 0, "connection refused"
    except Exception:
        return 0, "connection refused"


def wait_for_ready_py(port, timeout=15):
    """Poll localhost:port until healthy or timeout. Returns (ready, status_code, detail)."""
    deadline = time.monotonic() + timeout
    code, detail = 0, "connection refused"
    while time.monotonic() < deadline:
        code, detail = probe_port(port, timeout=2)
        if 200 <= code < 400:
            return True, code, detail
        time.sleep(1)
    return False, code, detail


# ── State helpers ───────────────────────────────────────────────────────────

def read_state():
    if not STATE_FILE.exists():
        return {"projects": {}}
    with open(STATE_FILE) as f:
        return json.load(f)


def write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(STATE_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2)


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
    # Escape single quotes for safe interpolation into ExecStart='...'
    safe_cmd = cmd.replace("'", "'\\''")
    svc_file.write_text(f"""[Unit]
Description=devtunnel app: {name} (port {port})
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory={directory}
ExecStart=/bin/bash -c '{safe_cmd}'
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
    code_count = sum(len(p.get("invite_codes", [])) for p in projects.values())
    ts_count = sum(1 for p in projects.values() if p.get("provider") == "tailscale")
    ts_hostname = detect_tailscale()
    # Read healthcheck state for unhealthy projects (handle both old/new format)
    unhealthy = []
    health_detail = {}
    hc_state_file = Path.home() / ".local" / "share" / "devtunnel" / "healthcheck-state.json"
    if hc_state_file.exists():
        try:
            hc_data = json.loads(hc_state_file.read_text())
            for hc_name, val in hc_data.items():
                if isinstance(val, (int, float)):
                    # Legacy format
                    if val > 0:
                        unhealthy.append(hc_name)
                    health_detail[hc_name] = {
                        "failures": int(val),
                        "last_status": None,
                        "last_check": None,
                        "last_restart": None,
                    }
                elif isinstance(val, dict):
                    failures = val.get("failures", 0)
                    if failures > 0:
                        unhealthy.append(hc_name)
                    health_detail[hc_name] = {
                        "failures": failures,
                        "last_status": val.get("last_status"),
                        "last_check": val.get("last_check"),
                        "last_restart": val.get("last_restart"),
                    }
        except (json.JSONDecodeError, AttributeError):
            pass

    return jsonify({
        "tunnel": get_tunnel_status(),
        "domain": DOMAIN,
        "tunnel_id": TUNNEL_ID,
        "project_count": project_count,
        "code_count": code_count,
        "tailscale_available": ts_hostname is not None,
        "tailscale_hostname": ts_hostname or "",
        "ts_project_count": ts_count,
        "unhealthy_projects": unhealthy,
        "health_detail": health_detail,
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
            "code_count": len(proj.get("invite_codes", [])),
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

        log_event("info", "add", name, f"provider=tailscale port={port} access={ts_mode}")
        url = ts_url(ts_hostname, ts_port)
        return jsonify({"ok": True, "url": url})

    else:
        # Cloudflare path
        proj_data = {
            "port": port,
            "provider": "cloudflare",
            "access": access,
            "dir": directory,
            "cmd": cmd,
        }
        if access == "locked":
            import hashlib
            salt = secrets.token_hex(16)
            password = secrets.token_hex(16)
            pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
            proj_data["auth_user"] = name
            proj_data["auth_pass"] = f"{salt}:{pw_hash}"
            proj_data["invite_codes"] = []
            # Ensure cookie_secret exists
            if "cookie_secret" not in state:
                state["cookie_secret"] = secrets.token_hex(32)
        state.setdefault("projects", {})[name] = proj_data
        write_state(state)
        rebuild_config()
        tunnel_msg = restart_tunnel()

        if directory and cmd:
            create_app_service(name, directory, cmd, port)

        log_event("info", "add", name, f"provider=cloudflare port={port} access={access}")
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

    # Remove app service if it exists
    remove_app_service(name)

    del state["projects"][name]
    write_state(state)

    tunnel_msg = ""
    if provider != "tailscale":
        rebuild_config()
        tunnel_msg = restart_tunnel()

    log_event("info", "remove", name, f"provider={provider}")
    return jsonify({"ok": True, "tunnel": tunnel_msg})


@app.route("/api/projects/<name>/health")
def api_project_health(name):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404
    port = state["projects"][name].get("port")
    if not port:
        return jsonify({"error": "No port configured"}), 400
    code, detail = probe_port(port)
    healthy = 200 <= code < 400
    return jsonify({"healthy": healthy, "status_code": code, "detail": detail})


@app.route("/api/projects/<name>/service/<action>", methods=["POST"])
def api_service_action(name, action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action. Use start, stop, or restart."}), 400

    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    proj = state["projects"][name]

    port = proj.get("port", 0)

    # If no service exists but dir+cmd are in state, create it on first start
    if action == "start" and get_app_service_status(name) == "none":
        directory = proj.get("dir", "")
        cmd = proj.get("cmd", "")
        if directory and cmd:
            create_app_service(name, directory, cmd, port)
            ready, hcode, detail = wait_for_ready_py(port, 15)
            log_event("info", "service_start", name, f"ready={ready} {detail}")
            return jsonify({
                "ok": True,
                "status": get_app_service_status(name),
                "health": {"ready": ready, "status_code": hcode, "detail": detail},
            })
        return jsonify({"error": "No service configured. Add --dir and --cmd to the project."}), 400

    svc_ok = control_app_service(name, action)
    log_event("info", f"service_{action}", name, f"ok={svc_ok}")

    result = {"ok": svc_ok, "status": get_app_service_status(name)}

    # Probe readiness after start/restart
    if action in ("start", "restart") and svc_ok and port:
        ready, hcode, detail = wait_for_ready_py(port, 15)
        result["health"] = {"ready": ready, "status_code": hcode, "detail": detail}
        log_event("info", f"service_{action}", name, f"ready={ready} {detail}")

    return jsonify(result)


@app.route("/api/tunnel/restart", methods=["POST"])
def api_restart_tunnel():
    rebuild_config()
    msg = restart_tunnel()
    log_event("info", "tunnel_restart", "", msg)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/operations-log")
def api_operations_log():
    limit = request.args.get("limit", 100, type=int)
    project_filter = request.args.get("project", "").strip()

    if not LOG_FILE.exists():
        return jsonify([])

    lines = LOG_FILE.read_text().strip().splitlines()
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if project_filter and entry.get("project") != project_filter:
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break

    return jsonify(entries)


# ── Code management endpoints ─────────────────────────────────────────────

def compute_code_status(code_entry):
    """Compute status for a code entry: active, exhausted, or expired."""
    max_uses = code_entry.get("max_uses")
    uses = code_entry.get("uses", 0)
    expires_at = code_entry.get("expires_at")

    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) >= exp_dt:
                return "expired"
        except (ValueError, AttributeError):
            pass

    if max_uses is not None and max_uses > 0 and uses >= max_uses:
        return "exhausted"

    return "active"


@app.route("/api/projects/<name>/codes")
def api_list_codes(name):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    codes = state["projects"][name].get("invite_codes", [])
    result = []
    for c in codes:
        entry = dict(c)
        entry["status"] = compute_code_status(c)
        result.append(entry)
    return jsonify(result)


@app.route("/api/projects/<name>/codes", methods=["POST"])
def api_add_code(name):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    data = request.json or {}
    no_email = data.get("no_email", False)
    email = data.get("email", "").strip() if not no_email else None
    max_uses = data.get("max_uses")
    if max_uses is not None:
        max_uses = int(max_uses)
    expires_at = data.get("expires_at", "").strip() or None

    if not no_email and not email:
        return jsonify({"error": "Email is required (or set no_email: true)"}), 400

    code = secrets.token_hex(6).upper()

    proj = state["projects"][name]
    if "invite_codes" not in proj:
        proj["invite_codes"] = []

    proj["invite_codes"].append({
        "code": code,
        "email": email,
        "expires_at": expires_at,
        "max_uses": max_uses,
        "uses": 0,
    })
    write_state(state)

    log_event("info", "code_add", name, f"code={code[:8]}... email={email or 'anonymous'}")
    return jsonify({"ok": True, "code": code})


@app.route("/api/projects/<name>/codes/<code>", methods=["PATCH"])
def api_edit_code(name, code):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    codes = state["projects"][name].get("invite_codes", [])
    matches = [c for c in codes if c["code"].startswith(code)]

    if len(matches) == 0:
        return jsonify({"error": f"No code matching prefix '{code}'"}), 404
    if len(matches) > 1:
        return jsonify({"error": f"Multiple codes match prefix '{code}'. Use a longer prefix."}), 400

    data = request.json or {}
    entry = matches[0]

    if "email" in data:
        entry["email"] = data["email"].strip() or None
    if "max_uses" in data:
        entry["max_uses"] = int(data["max_uses"]) if data["max_uses"] is not None else None
    if "expires_at" in data:
        entry["expires_at"] = data["expires_at"].strip() or None
    if data.get("reset_uses"):
        entry["uses"] = 0

    write_state(state)
    log_event("info", "code_edit", name, f"prefix={code[:8]}")
    return jsonify({"ok": True})


@app.route("/api/projects/<name>/codes/<code>", methods=["DELETE"])
def api_delete_code(name, code):
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    codes = state["projects"][name].get("invite_codes", [])
    matches = [c for c in codes if c["code"].startswith(code)]

    if len(matches) == 0:
        return jsonify({"error": f"No code matching prefix '{code}'"}), 404
    if len(matches) > 1:
        return jsonify({"error": f"Multiple codes match prefix '{code}'. Use a longer prefix."}), 400

    state["projects"][name]["invite_codes"] = [c for c in codes if not c["code"].startswith(code)]
    write_state(state)

    log_event("info", "code_rm", name, f"prefix={code[:8]}")
    return jsonify({"ok": True})


@app.route("/api/projects/<name>", methods=["PATCH"])
def api_update_project(name):
    data = request.json or {}
    state = read_state()
    if name not in state.get("projects", {}):
        return jsonify({"error": f"Project '{name}' not found"}), 404

    proj = state["projects"][name]
    changes = []

    new_cmd = data.get("cmd", "").strip()
    new_dir = data.get("dir", "").strip()
    new_port = data.get("port")

    if not new_cmd and not new_dir and new_port is None:
        return jsonify({"error": "At least one of cmd, dir, or port is required"}), 400

    if new_cmd:
        proj["cmd"] = new_cmd
        changes.append(f"cmd={new_cmd}")
    if new_dir:
        if not Path(new_dir).is_dir():
            return jsonify({"error": f"Directory does not exist: {new_dir}"}), 400
        proj["dir"] = new_dir
        changes.append(f"dir={new_dir}")
    if new_port is not None:
        try:
            new_port = int(new_port)
            if new_port < 1 or new_port > 65535:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "Port must be 1-65535"}), 400
        proj["port"] = new_port
        changes.append(f"port={new_port}")

    write_state(state)

    # Recreate service if cmd+dir are both set and either changed
    if (new_cmd or new_dir) and proj.get("cmd") and proj.get("dir"):
        create_app_service(name, proj["dir"], proj["cmd"], proj["port"])
        subprocess.run(
            ["systemctl", "--user", "restart", service_name(name)],
            capture_output=True
        )
        changes.append("service restarted")

    # Rebuild tunnel config if port changed (CF projects)
    if new_port is not None:
        provider = proj.get("provider", "cloudflare")
        if provider != "tailscale":
            rebuild_config()
            restart_tunnel()
        # Recreate service if only port changed but cmd+dir exist
        if not new_cmd and not new_dir and proj.get("cmd") and proj.get("dir"):
            create_app_service(name, proj["dir"], proj["cmd"], proj["port"])
            subprocess.run(
                ["systemctl", "--user", "restart", service_name(name)],
                capture_output=True
            )
            changes.append("service restarted")

    log_event("info", "set", name, " ".join(changes))
    return jsonify({"ok": True, "changes": changes})


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  devtunnel web UI")
    print(f"  Domain: {DOMAIN}")
    print(f"  State:  {STATE_FILE}")
    print(f"  Config: {TUNNEL_CONFIG}\n")
    app.run(host="0.0.0.0", port=7000, debug=False)
