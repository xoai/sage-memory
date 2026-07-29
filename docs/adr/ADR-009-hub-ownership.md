# ADR-009 — Hub ownership: PID + heartbeat writer discipline

**Status:** Accepted (rev 3 — heartbeat displacement handling)
**Cited by:** `hub/ownership.py`, `graph.py`, `db.py`, `cli_hub.py`,
`Dockerfile.slim`

## Context

Multiple sage-memory processes (servers, CLI invocations) can point
at the same hub-registered project DB. SQLite single-writer semantics
plus NFS/Docker filesystems make concurrent writes corrupting. This
is **concurrent-writer safety, NOT access control** (§"Scope
clarification") — reads are never restricted, and network access
control is a separate layer (P0-3 bearer auth).

## Decision

Applies only to hub-registered `writable: true` project DBs; the
global DB is never affected.

- **State file**: `<project>/.sage-memory/.hub-owner.json` carrying
  `owner_pid`, `owner_started`, `owner_heartbeat`.
- **Constants**: stale window 60s (generous tolerance for laptop
  sleep / clock skew / NTP jumps on Docker volumes); heartbeat
  interval 20s.
- **Acquire**: atomic `O_CREAT|O_EXCL`. Fresh claim by another live
  process → refuse. Stale claim → **atomic-rename reclaim (§3)**:
  each reclaimer writes its own candidate file, then `os.replace`s
  it onto the canonical path (POSIX rename is atomic — one payload
  wins, never a merge); read-after-write confirms the winner. Up to
  3 retries for legitimate-release races. Corrupt owner file →
  refuse silently (operator escape hatch: `rm` the file).
- **Write gating (§4, rev 2 per-DB)**: on first `get_db()` call per
  project, populate a per-DB `_disabled_writes` cache so hot write
  entry-points skip the file read. Owner pid == self → always a
  writer. Blocked writes return a read-only error envelope naming
  the owner PID and the remedy (stop the server or wait out the 60s
  stale window).
- **Heartbeat loop (rev 3)**: one asyncio task per token under the
  server lifespan; writes the heartbeat **only if still the recorded
  owner** (laptop-sleep edge case — a displaced former owner must
  not resurrect its claim); displacement → stop quietly;
  cancellation → release.
- **Release**: voluntary delete of owner file + registry entry;
  idempotent (missing file/entry are no-ops); `hub release <name>`
  matches this contract.
- **Escape hatches** (§"Escape hatches"): `SAGE_HUB_IGNORE_OWNERSHIP=1`
  (dev only), stale-wait 60s, manual `rm` of the owner file.

## Consequences

- `hub store`/`hub import` acquire ownership before writing and
  release after (ADR-008 §5/§6); server-owned projects keep
  long-lived tokens.
- Write entry-points (`store`, `graph.link`) consult
  `block_envelope_if_disabled` and return a structured envelope, not
  an exception.

## Gaps

Why 60s/20s specifically (vs e.g. 90s/30s) — tolerance rationale
recorded, exact numbers are judgment calls.
