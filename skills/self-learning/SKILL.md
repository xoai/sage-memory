---
name: self-learning
description: >
  Captures agent mistakes, corrections, and discovered gotchas so they are
  not repeated. Use when: (1) a command or operation fails unexpectedly,
  (2) the user corrects the agent, (3) the agent discovers non-obvious
  behavior through debugging, (4) an API or tool behaves differently than
  expected, (5) a better approach is found for a recurring task. Also
  searches past learnings before starting tasks to avoid known pitfalls.
  Activate alongside the memory skill — they share sage-memory but serve
  different purposes (memory = codebase knowledge, self-learning = agent
  mistakes and gotchas).
version: "1.2.0"
type: process
---

# Self-Learning

Learn from mistakes. Don't repeat them.

Captures what went wrong, what was non-obvious, and what the agent
should do differently. Every learning includes a **prevention rule** —
a forward-looking instruction that changes future behavior.

**Part of the unified knowledge system.** Self-learning stores through
sage-memory (or files) with the `self-learning` tag / `learning` type.
During recall, learnings surface as warnings alongside regular knowledge.

## Capabilities by Backend

| Capability | MCP | Files |
|------------|-----|-------|
| Store learnings | ✅ `sage_memory_store` | ✅ `.sage-memory/lrn-*.md` files |
| Search learnings | ✅ BM25 + `filter_tags` | ⚠️ scan `lrn-` files by name |
| Update learnings | ✅ `sage_memory_update` | ✅ edit file |
| Delete learnings | ✅ `sage_memory_delete` | ✅ delete file |
| Browse by type | ✅ `sage_memory_list` | ✅ scan `lrn-` files |
| Link to entities | ✅ `sage_memory_link` | ❌ skip |
| Graph-based recall | ✅ `sage_memory_graph` | ❌ skip |
| Namespace isolation | ✅ `filter_tags` | ✅ `lrn-` filename prefix |

**How to detect backend:** Try calling `sage_memory_search`. If it
responds, use MCP. If not, use `.sage-memory/` files.

## Recall: Search Before You Work

At task start, search for learnings relevant to the current task.

### With MCP

**Basic recall (keyword):**
```
sage_memory_search(
  query: "<task-relevant keywords>",
  filter_tags: ["self-learning"],
  limit: 5
)
```

Always include `filter_tags: ["self-learning"]` — this excludes all
non-learning entries.

**Targeted recall (graph-based):** When you know the current task's
ontology entity ID:
```
sage_memory_graph(
  id: "<task_entity_memory_id>",
  relation: "applies_to",
  direction: "inbound",
  depth: 1
)
```

Returns learnings explicitly linked to this task — more precise than
keyword search.

**Hot spot detection:**
```
sage_memory_graph(
  id: "<module_entity_id>",
  relation: "applies_to",
  direction: "inbound",
  depth: 1
)
```
If 5+ linked learnings → flag the area as mistake-prone.

### With Files

Scan `.sage-memory/` for `lrn-` prefixed files. Read filenames and
identify those relevant to the current task. Read matching files for
their prevention rules.

For a broad search: list all `lrn-*.md` files and scan names.
For a focused search: look for keywords in filenames like
`lrn-stripe-webhook-*.md` when working on Stripe webhooks.

### Reporting

When learnings are found, report the **prevention rule**, not the
incident. Say: "Before working with Stripe webhooks, verify that body
parsing middleware is skipped for the webhook route."

When nothing is found, say nothing.

## Capture: Detect and Store

### Five Learning Types

| Type | Trigger |
|------|---------|
| `gotcha` | Non-obvious behavior discovered through debugging |
| `correction` | User corrected the agent |
| `convention` | Undocumented project/team pattern discovered |
| `api-drift` | API/library behaves differently than expected |
| `error-fix` | Recurring error with a known solution |

### How to Store

**Title:** `[LRN:<type>] <specific description>`

**Content:** Four-part structure:
1. **What happened** — the symptom
2. **Why it was wrong** — root cause
3. **What's correct** — the right approach
4. **Prevention** — what to check BEFORE this happens again

**With MCP:**
```
sage_memory_store(
  title: "[LRN:gotcha] Stripe webhook requires raw body before JSON parsing",
  content: "What happened: Webhook signature verification failed with 400.
    Why: Express body parser replaced raw body with parsed JSON.
    What's correct: Use express.raw() for the webhook route.
    Prevention: Before implementing any webhook handler that verifies
    signatures, check whether the SDK requires the raw request body.",
  tags: ["self-learning", "gotcha", "stripe", "webhooks"],
  scope: "project"
)
```

**With files:**
```
File: .sage-memory/lrn-stripe-webhook-raw-body.md

---
tags: [self-learning, gotcha, stripe, webhooks]
type: learning
scope: project
created: 2026-03-20
---

[LRN:gotcha] Stripe webhook requires raw body before JSON parsing

What happened: Webhook signature verification failed with 400
"No signatures found matching the expected signature."

Why: Express body parser replaced raw body with parsed JSON before
the Stripe SDK could verify the signature.

What's correct: Use express.raw({type: 'application/json'})
middleware for the webhook route, before the global body parser.

Prevention: Before implementing any webhook handler that verifies
signatures (Stripe, GitHub, Twilio), check whether the SDK requires
the raw request body. If yes, ensure body parsing middleware is
skipped or deferred for that route.
```

### Link to Ontology Entities (MCP only)

After storing a learning, link it to the relevant entity:
```
sage_memory_link(
  source_id: "<learning_memory_id>",
  target_id: "<task_or_module_entity_id>",
  relation: "applies_to"
)
```

**With files:** Skip linking. Mention the related entity in the content
if the connection is important: "Related entity: task_a1b2 (Fix payment
timeout)."

### Search Before Store

Before creating a new learning, check for duplicates:

**With MCP:**
```
sage_memory_search(query: "<key terms>", filter_tags: ["self-learning"], limit: 5)
```

**With files:** Scan `lrn-*.md` filenames for similar topics.

If similar exists → update it. Don't create near-duplicates.

### When NOT to Store

Ask: "Would this change how I approach a future task?"
- **No** → don't store
- **Yes** → store

**Budget:** 2-5 learnings per significant task.

## Review: Curate and Improve

Triggered by "sage review" or "review learnings."

### With MCP

1. **Inventory** — `sage_memory_list(tags: ["self-learning"])` → all learnings
2. **By type** — `sage_memory_list(tags: ["self-learning", "gotcha"])` etc.
3. **Stale check** — flag learnings about changed code or outdated APIs
4. **Consolidate** — merge 3+ similar → store consolidated → link to same
   entities → delete originals
5. **Promote** — identify learnings for scope escalation
6. **Hot spots** — `sage_memory_graph` on key entities → count inbound
   `applies_to` edges → report most mistake-prone areas

### With Files

1. **Inventory** — list all `lrn-*.md` files
2. **By type** — read frontmatter to group by type tag
3. **Stale check** — check creation dates, read content for outdated refs
4. **Consolidate** — manually merge file contents → create new file →
   delete originals
5. **Promote** — identify candidates, create global-scope copy
6. **Hot spots** — count `lrn-*.md` files by domain keyword in filename

## Promote: Scope Escalation

**Project → Global:** Learning applies beyond this codebase. Store a
context-independent version at global scope.

**With MCP:** `sage_memory_store(..., scope: "global")`
**With files:** Copy to `~/.sage-memory/` (global directory), remove
project-specific details.

**Global → Team:** Export to a shared file in the repo. **Read:**
`references/team-sharing.md`.

**Read:** `references/promotion-rules.md` for criteria.

## Quality Principles

**Prevention over documentation.** Every learning answers: "What should
I check before this happens again?"

**Specificity retrieves.** `[LRN:gotcha] Stripe webhook requires raw body`
retrieves. `[LRN:gotcha] API issue` does not.

**Freshness matters.** Update or delete when code changes make a learning
obsolete.

**Learnings are not memories.** "Billing uses saga pattern" is a memory.
"Agent assumed REST, broke the compensation chain" is a learning.

## References

- `references/capture-patterns.md` — Triggers, examples, prevention rules
- `references/storage-conventions.md` — Format conventions
- `references/promotion-rules.md` — Scope escalation criteria
- `references/team-sharing.md` — Export formats for teams
- `references/review-workflow.md` — Curation process
- `references/examples.md` — End-to-end scenarios
- `references/ontology-integration.md` — Graph integration
