# Self-hosted Server Guide

Run the sage-memory MCP server as a shared service for your team
(Pattern B + C from [team-setup.md](team-setup.md)). 0.13.0+.

This guide covers:

- [Quick start](#quick-start) — one-host setup
- [Docker](#docker) — slim + full images
- [Reverse-proxy patterns](#reverse-proxy-patterns) — Caddy + TLS,
  Tailscale, Cloudflare Access
- [Pattern A → B migration](#pattern-a--b-migration) — move from
  the filesystem-shared global DB to a shared server
- [Subscription auth](#subscription-auth-for-the-worker) — share
  the dedup worker's LLM access via OAuth instead of API keys
- [Writer-discipline & ownership escape hatches](#ownership-escape-hatches)
- [Security model](#security-model) — what this guide DOES and DOES NOT
  promise

## Quick start

```bash
# Install on the host
pipx install 'sage-memory[neural]'

# Start the SSE transport on localhost
sage-memory serve --transport sse --port 3333 --hub

# (in a developer's MCP client)
# Point your client at http://<server-host>:3333/sse
```

The `--hub` flag activates hub-federation tools (cross-project
search and routed-write); see [team-setup.md](team-setup.md#pattern-c)
for the agent-side configuration.

## Docker

Two image variants ship from this cycle:

| Image            | Includes                            | Size (uncompressed, CI-measured) |
|------------------|-------------------------------------|----------------------------------|
| `sage-memory:slim` | Just sage-memory (no fastembed)     | ~210MB      |
| `sage-memory:full` | sage-memory + bge-small bundled     | ~455MB     |

Use **slim** when the host has good outbound network access — the
fastembed model downloads on first use (~30s warm-up). Use **full**
for offline / air-gapped / regulated environments.

```bash
# Build locally (or pull from your registry)
docker build -f Dockerfile.slim -t sage-memory:slim .
docker build -f Dockerfile.full -t sage-memory:full .

# Run — mount the state volume so DBs survive container restarts.
# SAGE_MEMORY_TOKEN is REQUIRED for non-loopback binds (0.0.0.0 in a
# container): without it the server refuses to start (P0-3).
docker run -d --name sage-memory \
  -e SAGE_MEMORY_TOKEN="$(openssl rand -hex 32)" \
  -v $HOME/.sage-memory:/root/.sage-memory \
  -p 127.0.0.1:3333:3333 \
  sage-memory:full \
  serve --transport sse --host 0.0.0.0 --port 3333 --hub
```

### ⚠️ Docker port-publish vs internal --host

**`docker run -p 3333:3333` publishes the container's port on
`0.0.0.0` of the host regardless of the internal `--host 127.0.0.1`
flag.** Docker's port-mapping operates at the host network layer,
not inside the container.

To keep the server **localhost-only on the host**, use the
`127.0.0.1:` prefix in the publish flag:

```bash
# Localhost-only (safe; SSH to access remotely)
docker run -p 127.0.0.1:3333:3333 sage-memory:full ...

# Wide-open (requires SAGE_MEMORY_TOKEN; see "Authentication")
docker run -p 3333:3333 sage-memory:full ...
```

For team setups exposing the server beyond `127.0.0.1`, deploy
a [reverse-proxy with auth](#reverse-proxy-patterns) in front.

## Authentication (P0-3)

Since 0.13.x the server **implements bearer-token authentication
itself** for the `sse`/`http` transports:

- Set `--token` or `SAGE_MEMORY_TOKEN`. Every request must then carry
  `Authorization: Bearer <token>` (compared with `hmac.compare_digest`)
  or it gets a 401.
- **Non-loopback binds refuse to start without a token.** Loopback
  (`127.0.0.1`, `localhost`, `::1`) and `stdio` stay zero-config.
- A **Host allowlist** (loopback spellings + `--allowed-host` /
  `SAGE_ALLOWED_HOSTS`) defeats DNS rebinding with 403; `Origin` is
  validated when present. Direct hostname/IP access needs an explicit
  `--allowed-host` — the allowlist is a browser defense; the token is
  the real gate.
- `set_project` is scoped to the detected project root subtree plus
  `SAGE_ALLOWED_ROOTS` (os.pathsep-separated); `~/.ssh`, `~/.gnupg`,
  `~/.aws`, and `/etc` are always denied.

Concurrent-writer safety is solved by the ownership protocol; network
access control is now built in, and a reverse proxy remains the
recommended pattern for TLS termination and SSO.

### Caddy + TLS (recommended for small teams)

```caddyfile
sage.your-team.com {
    reverse_proxy 127.0.0.1:3333
    basicauth /sse {
        team_member $2a$14$...  # bcrypt-hashed password
    }
}
```

Caddy auto-provisions TLS certs from Let's Encrypt. Pair with
HTTP Basic Auth for a 3-15 person team.

### Tailscale (recommended for medium teams)

```bash
# Server-side: install Tailscale, then bind sage-memory to the
# Tailscale interface only.
sage-memory serve --transport sse \
  --host $(tailscale ip -4) --port 3333 --hub
```

Tailscale handles auth + encryption between every team member. No
public DNS exposure. ACLs in the Tailscale admin panel restrict
who can reach the server.

### Cloudflare Access (recommended for larger teams)

Front the server with a Cloudflare Tunnel + Access policy. Team
members authenticate via SSO (Google Workspace, Okta, etc.) before
reaching the sage-memory server. Server itself binds to `127.0.0.1`
on the host; the tunnel daemon (cloudflared) is the only client.

## Pattern A → B migration

If your team already runs **Pattern A** (filesystem-shared global DB
via Syncthing), migrate to **Pattern B** (shared server) as follows:

```bash
# 1. Stop Syncthing for the .sage-memory/ folder on every machine
#    (prevents conflicts during the copy)

# 2. On the server host, install sage-memory
pipx install 'sage-memory[neural]'

# 3. Copy ONE team member's global DB to the server's home
mkdir -p ~/.sage-memory
scp $LAPTOP:~/.sage-memory/memory.db ~/.sage-memory/

# 4. Initialize the hub with the projects you want to federate
sage-memory hub init
sage-memory hub add ~/projects/backend --writable --name backend
sage-memory hub add ~/projects/frontend --writable --name frontend

# 5. Start the server
sage-memory serve --transport sse --port 3333 --hub

# 6. On each team member's laptop, update the MCP client config
#    to point at the server's SSE endpoint (instead of the local
#    sage-memory command). Remove the .sage-memory/ folder from
#    Syncthing.
```

## Subscription auth for the worker

The optional `sage-memory dedup` worker uses an LLM to confirm
entity merges (per ADR-003). By default, it reads
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the server's
environment. 0.13.0 adds an OAuth-based alternative so teams can
reuse personal subscriptions (ChatGPT Pro, Claude Max) instead
of procuring shared API keys.

### Enable subscription auth

1. **Opt in via config**:

   ```yaml
   # ~/.sage-memory/config.yaml on the server host
   auth:
     worker_llm:
       openai: subscription
       # claude: env  # leave as env to use ANTHROPIC_API_KEY
   ```

2. **Login** (opens the provider's OAuth flow in a browser):

   ```bash
   sage-memory auth login --provider openai
   ```

3. **OR import** existing CLI tool credentials:

   ```bash
   sage-memory auth import --provider openai   # reads ~/.codex/auth.json
   sage-memory auth import --provider claude   # reads ~/.claude/auth.json
   ```

4. **Check status**:

   ```bash
   sage-memory auth status
   sage-memory auth list
   ```

### Security model

- Credentials live at `~/.sage-memory/auth.json` with **mode 0600**.
  The file is rewritten via atomic mkstemp + os.replace so no
  partial writes are visible to concurrent readers.
- Subscription auth is **per-server-user, NOT per-MCP-client**. Each
  developer's local agent doesn't authenticate against the server's
  LLM provider — the server itself does.
- Token refresh fires automatically on 401; if the refresh token is
  revoked, the operator must re-run `sage-memory auth login`.
- The `auth.json` file is **NOT included in Docker images**. Mount
  the `~/.sage-memory/` volume to persist credentials.

## Ownership escape hatches

Per ADR-009 rev 2, hub-registered writable projects use a
`.hub-owner.json` heartbeat file to prevent multi-writer corruption.
Three documented escape hatches handle stuck states:

1. **Manual delete** — emergency recovery when a crashed server
   left a stale ownership file behind:

   ```bash
   rm <project>/.sage-memory/.hub-owner.json
   ```

   The next server startup proceeds normally. Safe because the
   60s heartbeat staleness check would have eventually reclaimed
   the file anyway.

2. **Voluntary release** — for planned migrations:

   ```bash
   sage-memory hub release backend    # releases the server's
                                       # held token for `backend`
   ```

   The running server gracefully releases ownership; another server
   (or `hub store --to backend` on the local stdio path) can now
   acquire it.

3. **Bypass for development** — when working on the server itself:

   ```bash
   SAGE_HUB_IGNORE_OWNERSHIP=1 sage-memory ...
   ```

   The stdio session bypasses the ownership check. **Dev-only;
   never recommended for production** — bypasses the multi-writer
   safety the protocol provides.

## Backup + restore

The state volume (`~/.sage-memory/`) contains:

- `memory.db` — the global memory DB
- `auth.json` — subscription credentials (if configured)
- `.sage-hub.yaml` is at `~/.sage-hub.yaml` (NOT inside `.sage-memory/`)

Project-level state (`<project>/.sage-memory/memory.db`) lives
under each registered project root.

Recommended backup target: rsync to a separate host, or a
filesystem snapshot. SQLite's WAL journal is compatible with
`rsync` while the server runs (read-consistent point-in-time copy).

## Security model — what this guide is NOT

This guide does NOT promise:

- **MCP-layer authentication.** sage-memory's server does not check
  who is making MCP requests. The reverse proxy / Tailscale ACL is
  the access-control layer.
- **Audit logging.** sage-memory does not record who wrote what.
  Use the reverse-proxy access logs or your SSO provider's audit
  trail for that.
- **End-to-end encryption to client agents.** TLS is the reverse
  proxy's job. The MCP-over-SSE / HTTP traffic between the proxy
  and sage-memory is plaintext over localhost.

For threat models where any of these is load-bearing, sage-memory's
hub federation is not the right primitive.
