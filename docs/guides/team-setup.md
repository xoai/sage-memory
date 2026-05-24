# sage-memory for Teams

sage-memory is designed for solo use by default, but its primitives
work for small teams of 3-15 people sharing institutional context
across machines. This guide covers the patterns that work **today**
on the shipped 0.12.x line, the trade-offs involved, and what's
on the roadmap for richer team support.

## What Teams Get

A shared sage-memory gives every team member and every AI agent
the same view of accumulated knowledge: architecture decisions,
debugging insights, project conventions, and prevention rules that
compound over time. New hires search the memory before asking
teammates. Agents read past corrections before repeating mistakes.

Concrete outcomes:

- **Onboarding context.** New engineers (and their agents) get
  searchable answers to "why did we choose X over Y?" without
  hunting Slack history.
- **Agent memory that compounds across the team.** When one
  agent learns a convention or hits a gotcha, every agent that
  reads the shared memory benefits.
- **Decisions don't get lost.** Architecture choices, API
  conventions, and post-incident learnings live in one place
  with typed graph edges between them.
- **Wrong answers can be invalidated, not just buried.** The
  self-learning skill's `corrects` relation + `status: invalidated`
  pattern stops a bad rule from re-surfacing. 0.12.0+ adds
  `supersedes` for semantic-paraphrase replacement.

## What Works Today vs What's Coming

| Capability | Today (0.12.x) | Roadmap |
|------------|----------------|---------|
| Per-user "team scope" DB | ✅ `scope: "global"` | — |
| Filesystem-shared global DB (Syncthing/NFS/OneDrive) | ✅ works for single-writer / mostly-reader teams | — |
| Per-project DB git-tracked | ⚠ feasible for tiny / read-heavy teams; concurrent writes risk SQLite conflicts | — |
| Shared MCP server (multiple agents over network) | ❌ stdio transport only | SSE / HTTP transport — see [.sage/work/20260524-team-mcp-transports/](../../.sage/work/20260524-team-mcp-transports/) |
| Federation across multiple project DBs | ❌ no `hub` primitive | Hub-style federation — same cycle |
| Subscription auth per-team-member | n/a (sage-memory has no required LLM dependency) | — |
| Web UI for browsing memory | ❌ MCP-only surface | not planned |

Patterns A and A' work today on every 0.12.x and 0.13.x release.
Patterns B (shared server) and C (federation) ship in 0.13.0.
The patterns below describe what's shippable today across the line.
Pattern B and C details from sister tools
like sage-wiki are queued as a separate architect cycle.

## Architecture Overview (today)

```
┌─────────────────────────────────────────────────────┐
│                    Team Members                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│  │ Dev + AI │  │ Dev + AI │  │ Dev + AI │           │
│  │  Agent   │  │  Agent   │  │  Agent   │           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘           │
│       │ MCP stdio   │ MCP stdio   │ MCP stdio       │
│       │ (local)     │ (local)     │ (local)         │
└───────┼─────────────┼─────────────┼─────────────────┘
        v             v             v
   ┌────────┐    ┌────────┐    ┌────────┐
   │  proj  │    │  proj  │    │  proj  │   per-project,
   │ sage.db│    │ sage.db│    │ sage.db│   per-machine
   └────────┘    └────────┘    └────────┘
        │             │             │
        v             v             v
   ┌──────────────────────────────────────┐
   │      ~/.sage-memory/sage.db          │   global scope —
   │  (per-user; placed on a shared       │   per-user, can sit
   │   filesystem via Syncthing / NFS /   │   on shared mount
   │   OneDrive for cross-machine reads)  │
   └──────────────────────────────────────┘
```

Both DBs are SQLite + sqlite-vec + FTS5 — same shape, same
schema, just different physical files. The `scope: "global"`
parameter on `sage_memory_store` / `sage_memory_search` /
`sage_memory_list` chooses which DB the call hits.

## Setup Patterns

### Pattern A: Per-User Global DB on a Shared Filesystem

The simplest team setup. Each team member writes `scope: "global"`
memories to `~/.sage-memory/sage.db`. The file lives on a shared
mount (Syncthing, NFS, OneDrive Files-On-Demand, Dropbox). Reads
work everywhere; **writes from one machine at a time** to avoid
SQLite WAL conflicts.

Best for teams of 3-8 where:
- One person is the primary "knowledge curator" (writes most
  team-scope memories)
- Others mainly search + occasionally write
- All machines mount the same filesystem path consistently

**Setup (Syncthing example):**

```bash
# On each team member's machine:
mkdir -p ~/Sync/sage-memory-team
# Configure Syncthing to share ~/Sync/sage-memory-team
# Symlink the team folder into sage-memory's expected location:
ln -s ~/Sync/sage-memory-team ~/.sage-memory
```

After Syncthing converges, all members read the same `sage.db`.
Project-scope memories (the default) still live in each
project's `.sage-memory/sage.db` per-machine — only the global
scope is shared.

**Daily workflow:**

```
# An agent stores team-scope context:
sage_memory_store(
  scope: "global",
  title: "API auth convention",
  content: "All endpoints expect Bearer JWT in Authorization header.
           Public endpoints documented in api/public.md. Reject any
           other auth method with 401.",
  tags: ["api", "auth", "convention"]
)

# Another team member's agent later:
sage_memory_search(query: "API auth", scope: "global")
# → finds the convention without anyone re-documenting it
```

**`.gitignore` for team Sync folder:**

```gitignore
# inside ~/Sync/sage-memory-team
*.db-shm
*.db-wal
```

The `.db-shm` / `.db-wal` files are per-process; only the
canonical `.db` should sync.

**Write contention:** SQLite is safe for concurrent reads but
two simultaneous writes across machines via a sync tool can
silently lose updates. Use one of these guards:
1. **Designated writer** — one team member's machine is the
   "writer," others promise to only `sage_memory_search`.
2. **Time-based handoff** — coordinate so writes happen at
   different times (morning standup writer, afternoon writer).
3. **Conflict-resolved store** — Syncthing's conflict files
   (`sync-conflict-...db`) flag collisions; resolve manually
   (rare with low write volume).

### Pattern A': Per-Project DB Git-Tracked

For teams that want to track their project-memory context
alongside the codebase, the per-project DB at
`{project_root}/.sage-memory/sage.db` can be added to the
repo — with caveats.

**When this works well:**
- Small teams (≤5) where one person does most writes
- Read-heavy workflow (agents search > store)
- Comfortable with occasional merge conflicts on the binary DB

**When this breaks down:**
- Multiple developers concurrently storing memories on different
  branches → git can't 3-way-merge a binary SQLite file
- The DB grows large (hundreds of MB of vectors) → repo bloat

**Setup:**

```bash
# in your project repo
mkdir -p .sage-memory
# remove this line from .gitignore if previously excluded:
# .sage-memory/
git add .sage-memory/
git commit -m "track shared sage-memory project DB"
```

**Mitigation: re-pull-then-write discipline.**

```
git pull --rebase
# ... store memories via your agent ...
git add .sage-memory/sage.db
git commit -m "add project memories: <topic>"
git push
```

If two developers commit memories concurrently, the second
`push` is rejected. The losing developer:
1. Backs up their just-stored memories (e.g., `sage_memory_list
   --since "2 hours ago"`)
2. `git reset --hard origin/main`
3. Re-stores the memories
4. Pushes again

This is friction-y. For anything beyond very small teams,
prefer Pattern A.

### Pattern B: Shared MCP Server (0.13.0+)

A single sage-memory process runs on a server; team members'
agents connect via MCP over SSE or HTTP rather than stdio.
Single writer = no contention; multiple readers fan out.

**Status:** **Shipped in 0.13.0.** See
[self-hosted-server.md](self-hosted-server.md) for the full
deployment guide (Docker, reverse-proxy, Tailscale, Cloudflare
Access patterns).

Quick start:

```bash
# On the server host
pipx install 'sage-memory[neural]'
sage-memory serve --transport sse --port 3333 --hub

# In each developer's MCP client config (e.g., claude_desktop_config.json)
# point at http://<server-host>:3333/sse
```

The `--hub` flag activates the hub-federation MCP tool surface
(`sage_memory_search(hub_projects=...)`,
`sage_memory_store(hub_target=...)`).

### Pattern C: Hub Federation (0.13.0+)

Large teams often have multiple project memories — one per
service or domain. The `hub` primitive lets `sage_memory_search`
fan out across multiple registered project DBs without forcing
them into a single shared file.

**Status:** **Shipped in 0.13.0.** Setup:

```bash
# On the server host
sage-memory hub init
sage-memory hub add ~/projects/backend --writable --name backend
sage-memory hub add ~/projects/frontend --searchable --name frontend
sage-memory hub add ~/projects/ops --writable --name ops

# Verify
sage-memory hub list
sage-memory hub status

# From an agent connected to the --hub server, search fans out:
#   sage_memory_search(query="auth runbook", hub_projects=["backend", "ops"])
# And routed writes:
#   sage_memory_store(content="...", hub_target="ops")
```

Writer-discipline (per-project ownership tokens; ADR-009) ensures
no two processes write to the same project DB concurrently. See
[self-hosted-server.md §Ownership escape hatches](self-hosted-server.md#ownership-escape-hatches)
for the three documented recovery paths.

## Configuring for Teams

### Recommended skill installation

Each team member runs:

```bash
sage-memory install-skills <agent> -y
```

This drops `sage-memory`, `sage-ontology`, and `sage-self-learning`
into the agent's skill directory. The shared skills teach every
agent on the team to:
- Search before answering ("Read before write" loop)
- Capture corrections as self-learnings
- Use typed relations (`depends_on`, `corrects`, `supersedes`, ...)
- Promote learnings from project → global scope when they're
  team-wide

### Promotion ladder

The README's stated mental model:

> Experience layer — every correction becomes a prevention rule;
> rules promote from context → personal → team scope.

Concretely:
- **Context-scope** — ephemeral, lives in the current agent
  session only (no `sage_memory_store` call needed).
- **Personal-scope** = `scope: "project"` — survives across the
  agent's sessions for this codebase. Most learnings start here.
- **Team-scope** = `scope: "global"` — when a learning generalizes
  beyond the project (e.g., a team-wide API convention, a
  language-feature gotcha), the agent or the developer
  re-stores it with `scope: "global"`.

For team setups using **Pattern A**, the global scope is the
shared one — promotion to team-scope means "now everyone on
the team sees this."

## Workflows

### Capturing decisions as a team

After an architecture review or post-incident retro:

```
# Agent stores the decision with typed entities + relations
sage_memory_store(
  scope: "global",
  title: "Use Postgres logical replication for cross-region read replicas",
  content: "<decision rationale, alternatives considered, trade-offs>",
  tags: ["architecture", "postgres", "replication"],
  entities: [
    {name: "Postgres", type: "TECHNOLOGY"},
    {name: "logical replication", type: "CONCEPT"},
    {name: "cross-region read replicas", type: "CONCEPT"}
  ],
  relations: [
    {source: "Postgres", rel: "supports", target: "logical replication"}
  ]
)
```

Future searches like `sage_memory_search(query: "read replica
options", scope: "global")` surface the decision + its rationale.

### Linking superseded conventions (0.12.0+)

When the team revises a convention, the new memory can supersede
the old one — both stay visible but search results flag the older
with `superseded_by: <newer_id>`:

```
# Old: store("Auth tokens expire in 24h")
# New: store("Auth tokens expire in 1h (changed 2026-Q2 per security review)")
# Then:
sage_memory_link(
  source_id: <new_id>,
  target_id: <old_id>,
  relation: "supersedes",
  scope: "global"
)
```

`sage_memory_search` returns both; the older now carries the
`superseded_by` pointer. The 0.12.0 semantic-dedup signal makes
this less manual: if the new memory's embedding is ≥ 0.95 cosine
to the old, `sage_memory_store`'s response already surfaces the
old memory as a `near_duplicate` candidate.

### Invalidating wrong learnings

When a stored memory turns out to be incorrect (a convention
changed, an API behavior was misunderstood), the self-learning
skill's pattern applies:

```
sage_memory_update(id: "<wrong_id>", status: "invalidated", scope: "global")
sage_memory_store(
  scope: "global",
  title: "[LRN:correction] The actual behavior is X, not Y",
  content: "<what happened, why the original was wrong, what's correct, prevention rule>",
  tags: ["self-learning", "correction"]
)
sage_memory_link(
  source_id: <correction_id>,
  target_id: <wrong_id>,
  relation: "corrects",
  scope: "global"
)
```

The invalidated memory disappears from future search results;
the correction stays active. See
[`src/sage_memory/skills/sage-self-learning/SKILL.md`](../../src/sage_memory/skills/sage-self-learning/SKILL.md)
for the full pattern.

## Trust Considerations for Teams

sage-memory has **no built-in trust system** like sage-wiki's
consensus quorum + grounding verification. Memories are accepted
on write; the only quality gates are:

1. **SHA-256 dedup** (always) + **semantic dedup signal** (0.12.0+)
   — agents see near-duplicates and decide whether to link via
   `supersedes`, merge, or store as distinct.
2. **`status: invalidated`** filter — invalidated memories don't
   surface in search.
3. **Tag filtering** — `filter_tags` on search.

For teams that need stronger trust gates (e.g., "no learning
enters team scope without a second human reviewer"), the simplest
pattern today is **convention-based**:

- Use **personal scope** by default. Store team-scope memories
  only via an explicit "promote" step (a separate agent action
  or CLI command the team agrees on).
- **Tag review status.** Apply `tags: ["pending-review"]` on
  team-scope stores; only remove the tag after review.
- **Use the `corrects` relation freely.** Teammates can always
  flag a memory wrong by storing a `correction` and linking it.

## Scaling Considerations

### Performance

| Memory count | DB size | Search p99 | Dedup p99 (0.12.0+) |
|--------------|---------|-----------|---------------------|
| 1K           | ~5 MB   | ~10ms     | ~1ms                |
| 10K          | ~50 MB  | ~50ms     | ~5ms (measured)     |
| 100K         | ~500 MB | ~200ms*   | ~50ms*              |

* extrapolated — not benchmarked. The semantic-dedup perf test
seeds 10k vectors; production teams should re-measure their
workload before assuming.

### When `global` scope gets too big

A `global` DB shared across a team for years can grow large
enough that searches slow down. Mitigations:

1. **Tag-filter searches.** `filter_tags: ["api"]` cuts the
   FTS / vector search space.
2. **Invalidate stale memories.** The `corrects` + `status:
   invalidated` pattern lets you retire memories that no longer
   apply without losing the historical record.
3. **Per-domain `global` DBs (manual federation).** Until
   Pattern C ships, you can run separate `~/.sage-memory/` paths
   per domain (frontend / backend / ops) by using different
   `HOME` env values or symlink swaps. Agents pick the right
   one per project.

## Troubleshooting

### "My teammates don't see my memories"

1. Confirm you used `scope: "global"`, not the default
   `scope: "project"`.
2. Confirm the global DB path is actually shared (run
   `realpath ~/.sage-memory/sage.db` on both machines — paths
   should resolve to the same physical file on the shared mount).
3. Confirm the sync tool has finished propagating (Syncthing
   shows status; OneDrive may take minutes).
4. Confirm no one else is currently writing (concurrent writes
   risk silent loss — see Pattern A's "Write contention" notes).

### "Git is fighting me about the .sage-memory directory"

You're hitting Pattern A's binary-DB merge problem. Either
switch to Pattern A (shared filesystem) or pick a designated
writer for the project DB.

### "I want a shared server like sage-wiki has"

That's Pattern B — not shipped yet. The architect cycle at
[.sage/work/20260524-team-mcp-transports/](../../.sage/work/20260524-team-mcp-transports/)
captures the scope. A `/architect` invocation in a fresh
session auto-picks up.

## See Also

- [sage-wiki's team-setup guide](https://github.com/xoai/sage-wiki/blob/main/docs/guides/team-setup.md)
  — sister tool with shipped Pattern B (shared server / SSE)
  and Pattern C (hub federation). The two tools share design
  DNA; sage-memory's team story will converge toward sage-wiki's
  as Patterns B + C land.
- [`src/sage_memory/skills/sage-self-learning/SKILL.md`](../../src/sage_memory/skills/sage-self-learning/SKILL.md)
  — the self-learning pattern (correction + invalidation +
  `corrects` / `supersedes` relations).
- [README.md](../../README.md) §"Setup" — basic install +
  per-project setup.
- [CHANGELOG.md](../../CHANGELOG.md) — what's shipped each
  version (semantic dedup landed in 0.12.0; tree-sitter codebase
  scan in 0.11.0).
