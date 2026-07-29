# ADR-010 — Subscription auth (OAuth) for the worker LLM

**Status:** Accepted
**Cited by:** `auth/` package, `llm.py`, `cli_auth.py`,
`__init__.py`

## Context

The optional worker LLM (entity extraction, dedup) previously
required raw API keys in env vars. Teams with Claude/OpenAI
**subscriptions** (not API billing) already hold OAuth grants in the
official CLI tools; asking for separate API keys blocks adoption.

## Decision

- **Opt-in, backwards compatible**: subscription auth activates via
  `~/.sage-memory/config.yaml` key `auth.worker_llm.<provider>:
  subscription`. Absent config = env-var-only behavior, unchanged.
  Subscription-opted-in without stored credentials **falls back to
  env var** with a once-per-provider WARNING (a half-configured
  worker is not dead-in-the-water).
- **OAuth flow** (ported from sage-wiki `internal/auth/oauth.go`):
  authorization-code flow; localhost callback server on a free port
  (`redirect_uri=http://localhost:<port>/callback`, stdlib
  `http.server` — no new dep); random `state` verified on callback
  (CSRF); 300s timeout; code exchanged at the provider token
  endpoint; `client_id="sage-memory"`; httpx for token calls so
  tests can `MockTransport`.
- **Providers** (mirrors sage-wiki `providers.go`): `openai` and
  `claude` — authorize/token endpoints, API base URL, scopes
  `("read","write")`, and CLI cred-file import paths
  (`.codex/auth.json`, `.claude/auth.json`).
- **Storage (§"Storage")**: `~/.sage-memory/auth.json`, strict mode
  **0600** (the file mode is part of the security contract; anything
  looser is a security bug), parent dir 0700, symlink targets
  rejected on load AND save (symlink-substitution attack), atomic
  temp + chmod + `os.replace` writes, `type` always
  `"subscription"`. Missing file = empty store (auth is opt-in).
- **Refresh**: `Authorization: Bearer <token>` injection on worker
  LLM calls; 30s expiry skew (refresh slightly early to avoid the
  valid-at-construction/expired-in-flight race); on 401 → refresh
  via refresh_token → persist → retry once; refresh-token rotation
  honored; failures raise `RefreshError`/`OAuthError` pointing at
  `sage-memory auth login --provider …`.
- **Import** (`auth import`): accepts relaxed shapes from CLI tools'
  cred files (`access_token|accessToken|token`, etc.), writes the
  canonical shape; missing expiry → now+3600 so the refresh path
  engages on first 401.
- **CLI**: `sage-memory auth {login, import, list, remove, status}`;
  hand-rolled flag parsing; `--auth-path` override.

## Consequences

- `llm.is_configured()` checks both paths per provider; pre-M4
  callers ("can the worker run an LLM at all?") are unaffected.
- Credentials never enter config files or logs; the 0600/0700
  contract is explicitly load-bearing (P0-3 guardrail).

## Gaps

Provider endpoints are correct as of 2026-05-24 but may change at
the provider's discretion; refresh-time failures surface clear
re-login errors by design.
