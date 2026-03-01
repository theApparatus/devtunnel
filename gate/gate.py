#!/usr/bin/env python3
"""
devtunnel auth gate — cookie-based auth proxy for locked projects.

Sits between frpc and the app:
    frpc → gate:7500 → app:PORT

Uses Host header to identify which project, validates a signed session cookie
(30-day expiry), shows a login page if unauthenticated. Supports password auth
and invite codes (one-click access via ?invite=CODE query param).

Proxies all HTTP traffic and relays WebSocket connections.

State is read from ~/.config/devtunnel/state.json (same as CLI + web UI).
"""

import hmac
import http.client
import http.server
import json
import os
import select
import socket
import struct
import hashlib
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# ── Config ──────────────────────────────────────────────────────────────────

GATE_PORT = 7500
STATE_FILE = Path.home() / ".config" / "devtunnel" / "state.json"
COOKIE_NAME = "devtunnel_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days
AUTH_PREFIX = "/__devtunnel_auth"

# ── State ───────────────────────────────────────────────────────────────────

def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"projects": {}}


def write_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def normalize_codes(codes):
    """Migrate plain string invite codes to objects."""
    return [
        c if isinstance(c, dict) else {"code": c, "expires_at": None, "max_uses": None, "uses": 0}
        for c in (codes or [])
    ]


def validate_invite_code(code_str, codes):
    """Check if an invite code is valid (exists, not expired, not exhausted). Returns the code object or None."""
    now = datetime.now(timezone.utc)
    for entry in codes:
        if entry["code"] != code_str:
            continue
        # Check expiry
        if entry.get("expires_at"):
            try:
                exp = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
                if now >= exp:
                    return None
            except (ValueError, AttributeError):
                pass
        # Check max uses
        if entry.get("max_uses") is not None and entry.get("uses", 0) >= entry["max_uses"]:
            return None
        return entry
    return None


def increment_invite_uses(project_name, code_str):
    """Increment the uses counter for an invite code in the state file."""
    state = read_state()
    proj = state.get("projects", {}).get(project_name)
    if not proj:
        return
    codes = normalize_codes(proj.get("invite_codes", []))
    for entry in codes:
        if entry["code"] == code_str:
            entry["uses"] = entry.get("uses", 0) + 1
            break
    proj["invite_codes"] = codes
    write_state(state)


def get_serializer():
    state = read_state()
    secret = state.get("cookie_secret")
    if not secret:
        raise RuntimeError("No cookie_secret in state. Run 'devtunnel add --locked' to initialize.")
    return URLSafeTimedSerializer(secret)


def find_project(host):
    """Find the project matching this Host header."""
    # Strip port from host
    hostname = host.split(":")[0] if host else ""
    # The subdomain is the project name (e.g. "myproject.app.fixshifted.com")
    subdomain = hostname.split(".")[0] if hostname else ""

    state = read_state()
    projects = state.get("projects", {})

    if subdomain in projects:
        return subdomain, projects[subdomain]

    return None, None


def check_cookie(cookie_header, project_name):
    """Check if the request has a valid session cookie for this project."""
    if not cookie_header:
        return False

    # Parse cookies manually (avoid pulling in http.cookies for simple case)
    cookies = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    token = cookies.get(COOKIE_NAME)
    if not token:
        return False

    try:
        s = get_serializer()
        data = s.loads(token, max_age=COOKIE_MAX_AGE)
        if data.get("project") != project_name:
            return False

        # Check auth_revoked_at — reject cookies issued before revocation
        state = read_state()
        proj = state.get("projects", {}).get(project_name, {})
        revoked_at = proj.get("auth_revoked_at")
        if revoked_at:
            iat = data.get("iat", 0)
            try:
                revoked_ts = datetime.fromisoformat(revoked_at.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                revoked_ts = 0
            if iat < revoked_ts:
                return False

        return True
    except (BadSignature, SignatureExpired):
        return False


def make_cookie(project_name):
    """Create a signed session cookie value."""
    s = get_serializer()
    token = s.dumps({"project": project_name, "iat": int(time.time())})
    return f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Lax; Secure"


def clear_cookie():
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


# ── Login page HTML ─────────────────────────────────────────────────────────

def login_page_html(project_name, error="", invite_ok=False):
    safe_name = html_escape(project_name)
    error_html = f'<div class="error">{html_escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — {safe_name}</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .login-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 32px;
    width: 380px;
    max-width: 90vw;
  }}
  .login-card h1 {{
    font-size: 20px;
    font-weight: 600;
    margin-bottom: 4px;
  }}
  .login-card .subtitle {{
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 24px;
  }}
  .form-group {{
    margin-bottom: 16px;
  }}
  .form-group label {{
    display: block;
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 6px;
    font-weight: 500;
  }}
  .form-group input {{
    width: 100%;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-size: 14px;
    outline: none;
  }}
  .form-group input:focus {{
    border-color: var(--accent);
  }}
  .btn {{
    width: 100%;
    padding: 10px;
    border-radius: 6px;
    border: 1px solid var(--accent);
    background: rgba(88, 166, 255, 0.15);
    color: var(--accent);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s;
  }}
  .btn:hover {{
    background: rgba(88, 166, 255, 0.25);
  }}
  .divider {{
    display: flex;
    align-items: center;
    margin: 20px 0;
    color: var(--text-muted);
    font-size: 12px;
  }}
  .divider::before, .divider::after {{
    content: '';
    flex: 1;
    border-bottom: 1px solid var(--border);
  }}
  .divider span {{
    padding: 0 12px;
  }}
  .error {{
    background: rgba(248, 81, 73, 0.1);
    border: 1px solid var(--red);
    color: var(--red);
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 13px;
    margin-bottom: 16px;
  }}
  .footer {{
    text-align: center;
    margin-top: 20px;
    font-size: 11px;
    color: var(--text-muted);
  }}
</style>
</head>
<body>
<div class="login-card">
  <h1>{safe_name}</h1>
  <div class="subtitle">This project requires authentication to access.</div>
  {error_html}
  <form method="POST" action="{AUTH_PREFIX}/login">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" autocomplete="username" required autofocus>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" autocomplete="current-password" required>
    </div>
    <button type="submit" class="btn">Sign in</button>
  </form>
  <div class="divider"><span>or</span></div>
  <form method="POST" action="{AUTH_PREFIX}/login">
    <div class="form-group">
      <label>Invite Code</label>
      <input type="text" name="invite_code" placeholder="Enter invite code" autocomplete="off">
    </div>
    <button type="submit" class="btn">Use Invite Code</button>
  </form>
  <div class="footer">devtunnel auth gate</div>
</div>
</body>
</html>"""


# ── WebSocket relay ─────────────────────────────────────────────────────────

def relay_websocket(client_sock, backend_sock):
    """Relay data between client and backend sockets using select."""
    sockets = [client_sock, backend_sock]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, 30.0)
            if errored:
                break
            for s in readable:
                data = s.recv(65536)
                if not data:
                    return
                target = backend_sock if s is client_sock else client_sock
                target.sendall(data)
    except (OSError, ConnectionError):
        pass
    finally:
        try:
            client_sock.close()
        except OSError:
            pass
        try:
            backend_sock.close()
        except OSError:
            pass


# ── Request Handler ─────────────────────────────────────────────────────────

class GateHandler(http.server.BaseHTTPRequestHandler):
    # Suppress default logging to stderr per-request; we log ourselves
    def log_message(self, format, *args):
        pass

    def do_request(self):
        host = self.headers.get("Host", "")
        project_name, project = find_project(host)

        if not project:
            self.send_error_page(404, "Project not found",
                                 f"No project matches host: {host}")
            return

        if project.get("access") != "locked":
            # Public project — should not be routed through gate, but proxy anyway
            self.proxy_request(project_name, project)
            return

        # Check for ?invite=CODE in query string (auto-authenticate)
        parsed = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed.query)
        invite_code = query_params.get("invite", [None])[0]

        if invite_code:
            codes = normalize_codes(project.get("invite_codes", []))
            entry = validate_invite_code(invite_code, codes)
            if entry:
                increment_invite_uses(project_name, invite_code)
                cookie = make_cookie(project_name)
                clean_path = parsed.path or "/"
                self.send_response(302)
                self.send_header("Location", clean_path)
                self.send_header("Set-Cookie", cookie)
                self.end_headers()
                return
            else:
                self.send_login_page(project_name, error="Invalid or expired invite code.")
                return

        # Handle auth routes
        if self.path.startswith(AUTH_PREFIX):
            self.handle_auth_route(project_name, project)
            return

        # Check cookie
        cookie_header = self.headers.get("Cookie", "")
        if check_cookie(cookie_header, project_name):
            self.proxy_request(project_name, project)
        else:
            self.send_login_page(project_name)

    # Alias all methods to do_request
    do_GET = do_request
    do_POST = do_request
    do_PUT = do_request
    do_DELETE = do_request
    do_PATCH = do_request
    do_HEAD = do_request
    do_OPTIONS = do_request

    def handle_auth_route(self, project_name, project):
        path = self.path.split("?")[0]  # strip query

        if path == f"{AUTH_PREFIX}/login" and self.command == "POST":
            self.handle_login(project_name, project)
        elif path == f"{AUTH_PREFIX}/logout":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", clear_cookie())
            self.end_headers()
        else:
            self.send_login_page(project_name)

    def handle_login(self, project_name, project):
        # Read POST body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        params = urllib.parse.parse_qs(body)

        username = params.get("username", [""])[0]
        password = params.get("password", [""])[0]
        invite_code = params.get("invite_code", [""])[0]

        # Try invite code first
        if invite_code:
            codes = normalize_codes(project.get("invite_codes", []))
            entry = validate_invite_code(invite_code, codes)
            if entry:
                increment_invite_uses(project_name, invite_code)
                cookie = make_cookie(project_name)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", cookie)
                self.end_headers()
                return
            else:
                self.send_login_page(project_name, error="Invalid or expired invite code.")
                return

        # Try password auth
        if username and password:
            expected_user = project.get("auth_user", "")
            expected_pass = project.get("auth_pass", "")
            if hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass):
                cookie = make_cookie(project_name)
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", cookie)
                self.end_headers()
                return

        self.send_login_page(project_name, error="Invalid username or password.")

    def send_login_page(self, project_name, error=""):
        html = login_page_html(project_name, error=error)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_page(self, code, title, message):
        safe_title = html_escape(str(title))
        safe_message = html_escape(str(message))
        html = f"""<!DOCTYPE html>
<html><head><title>{safe_title}</title>
<style>
body {{ font-family: sans-serif; background: #0d1117; color: #e6edf3;
       display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
.box {{ text-align: center; }}
h1 {{ font-size: 48px; color: #8b949e; }}
p {{ color: #8b949e; }}
</style></head><body><div class="box"><h1>{code}</h1><p>{safe_message}</p></div></body></html>"""
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy_request(self, project_name, project):
        """Proxy the request to the app's local port."""
        port = project["port"]

        # Check for WebSocket upgrade
        upgrade = self.headers.get("Upgrade", "").lower()
        if upgrade == "websocket":
            self.proxy_websocket(port)
            return

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)

            # Forward headers (filter out hop-by-hop)
            headers = {}
            hop_by_hop = {"connection", "keep-alive", "proxy-authenticate",
                          "proxy-authorization", "te", "trailers",
                          "transfer-encoding"}
            for key, val in self.headers.items():
                if key.lower() not in hop_by_hop:
                    headers[key] = val

            # Read request body if present
            body = None
            content_length = self.headers.get("Content-Length")
            if content_length:
                body = self.rfile.read(int(content_length))

            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            # Read full response body (handles chunked decoding internally)
            resp_body = resp.read()

            # Send response back to client
            self.send_response(resp.status)
            # Forward response headers, replacing transfer-encoding with
            # explicit content-length so the client knows when body ends
            skip = {"transfer-encoding", "content-length", "connection"}
            for key, val in resp.getheaders():
                if key.lower() not in skip:
                    self.send_header(key, val)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

            conn.close()

        except (ConnectionRefusedError, OSError) as e:
            self.send_error_page(502, "Bad Gateway",
                                 f"Cannot connect to {project_name} on port {port}. Is the app running?")

    def proxy_websocket(self, port):
        """Proxy WebSocket connections by raw socket relay."""
        try:
            # Connect to backend
            backend = socket.create_connection(("127.0.0.1", port), timeout=10)

            # Reconstruct the original HTTP upgrade request for the backend
            request_line = f"{self.command} {self.path} {self.request_version}\r\n"
            headers = ""
            for key, val in self.headers.items():
                headers += f"{key}: {val}\r\n"
            raw_request = (request_line + headers + "\r\n").encode()
            backend.sendall(raw_request)

            # Get the client's underlying socket
            client_sock = self.connection

            # Relay in a thread (this blocks the handler thread — that's fine,
            # ThreadingHTTPServer gives us one thread per connection)
            relay_websocket(client_sock, backend)

        except (ConnectionRefusedError, OSError):
            self.send_error_page(502, "Bad Gateway",
                                 f"Cannot connect to WebSocket on port {port}.")


# ── Server ──────────────────────────────────────────────────────────────────

class GateServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = GateServer(("127.0.0.1", GATE_PORT), GateHandler)
    print(f"devtunnel auth gate listening on 127.0.0.1:{GATE_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down gate...")
        server.shutdown()


if __name__ == "__main__":
    main()
