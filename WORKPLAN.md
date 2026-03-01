# devtunnel: Cloudflare → frp Migration Work Plan

## Goal

Replace Cloudflare Tunnel + Access with frp on Fly.io, preserving the existing `devtunnel` CLI and web UI experience.

## Decisions

| Decision | Answer |
|---|---|
| Tunnel protocol | frp (WebSocket transport) |
| Edge proxy | frps Go binary on Fly.io |
| Auth | HTTP basic auth, multiple users per project (invite list) |
| DNS | Cloudflare DNS — wildcard `*.fixshifted.com`, one subdomain per project |
| Domain pattern | `projectname.fixshifted.com` (use `projectname-api` etc. for extra subdomains) |
| Build vs adopt | Adopt frp, wrap with existing CLI |
| frpc bundling | Auto-install frpc from devtunnel |
| frps updates | Automated deploys on Fly.io |
| Fallback | frp only — hard cut, no Cloudflare backend |

## Scope
- **In scope**: frps deployment on Fly.io, frpc integration on dev machine, CLI rewrite to use frpc admin API, web UI migration, per-project multi-user basic auth, wildcard DNS/TLS setup
- **Out of scope**: Multi-tenant support, custom auth UI, mobile access, frp dashboard exposure to the internet

## Phases

### Phase 1: Deploy frps on Fly.io
**Priority**: Critical — everything else depends on this

- [ ] Create `fly/` directory with Fly.io deployment files
- [ ] Write `Dockerfile` — download frps binary, minimal image
- [ ] Write `frps.toml` config: bindPort, vhostHTTPPort, subdomainHost=fixshifted.com, auth.token, WebSocket transport
- [ ] Write `fly.toml` with app config, services, health checks
- [ ] Create Fly.io app and deploy (`fly launch` / `fly deploy`)
- [ ] Set up wildcard DNS on Cloudflare: `*.fixshifted.com` CNAME → fly app
- [ ] Set up wildcard TLS cert on Fly: `fly certs create "*.fixshifted.com"`
- [ ] Smoke test: manually run frpc locally, verify a test subdomain proxies through

**Deliverable**: Working frps on Fly.io accepting tunnel connections, wildcard DNS + TLS operational

### Phase 2: frpc systemd service on dev machine
**Priority**: Critical

- [ ] Add frpc auto-install logic to devtunnel CLI (download binary to ~/.local/bin or similar)
- [ ] Write base `frpc.toml` config: server address, auth token, WebSocket transport, admin API on 127.0.0.1:7400
- [ ] Create systemd user service for frpc (replaces cloudflared service)
- [ ] Verify auto-reconnect after sleep/wake
- [ ] Test admin API: `curl http://127.0.0.1:7400/api/config`

**Deliverable**: frpc running as a systemd service with admin API accessible locally

### Phase 3: Rewrite CLI (`bin/devtunnel`)
**Priority**: High

- [ ] Replace Cloudflare env vars with frp config (FRP_SERVER_ADDR, FRP_AUTH_TOKEN)
- [ ] Rewrite `add`: POST proxy config to frpc admin API (subdomain, localPort, optional auth)
- [ ] Rewrite `remove`: remove proxy via frpc admin API
- [ ] Rewrite `list`: query frpc admin API for active proxies
- [ ] Rewrite `restart`: reload frpc config via admin API
- [ ] Remove `reset-dns` and `setup-otp` commands
- [ ] Update state file format: drop Cloudflare Access IDs, add per-project user list
- [ ] Add `invite` subcommand: add/remove username+password for a project (multi-user)
- [ ] Keep `start`/`stop`/`logs`/`run` as-is (app process management, not tunnel)
- [ ] Auto-install frpc on first run if missing

**Deliverable**: CLI fully functional against frp backend

### Phase 4: Rewrite web UI (`web/app.py`)
**Priority**: High

- [ ] Replace Cloudflare API calls with frpc admin API calls
- [ ] Update project CRUD to use frpc admin API
- [ ] Replace email whitelist UI with username/password invite management
- [ ] Update status endpoint to check frpc connection
- [ ] Remove all Cloudflare Access logic

**Deliverable**: Web UI fully functional with frp backend

### Phase 5: Update MCP plugin
**Priority**: Medium

- [ ] Update tool definitions (drop setup_otp, reset_dns; add invite)
- [ ] Update descriptions to reference frp
- [ ] Test all MCP tools end-to-end

**Deliverable**: MCP plugin works with new backend

### Phase 6: Cleanup
**Priority**: Low

- [ ] Remove all Cloudflare code paths
- [ ] Remove cloudflared dependency
- [ ] Update config templates
- [ ] Tag a release

**Deliverable**: Clean codebase, no Cloudflare dependencies

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| frp WebSocket unreliable through Fly proxy | Tunnel drops | Test early in Phase 1; fallback to TCP with dedicated port |
| frpc admin API lacks needed features | Can't dynamically manage proxies | frpc supports config hot-reload via PUT /api/config |
| Basic auth UX worse than email OTP | Friction for shared access | Can add OAuth proxy later |
| Laptop sleep kills tunnel | Projects go offline | frpc has built-in reconnect with retry |
