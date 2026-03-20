# Storage Protocol

The contract between sage-memory and skills that build on it. Follow
these conventions for consistent storage, reliable retrieval, and
cross-skill interoperability.

## Tag Conventions

Tags serve three purposes: domain classification, namespace isolation,
and entity linking. Skills must use tags consistently for `filter_tags`
and `sage_memory_list` to work correctly.

### Reserved Tags

| Tag | Meaning | Used by |
|-----|---------|---------|
| `ontology` | Entry is an ontology entity or relation | ontology skill |
| `entity` | Ontology entity (always paired with type tag) | ontology skill |
| `self-learning` | Entry is a learning | self-learning skill |
| `schema` | Ontology type/relation definition | ontology skill |

### Namespace Pattern

Every skill that stores distinguishable entries should use a namespace
tag as the **first tag**:

```
tags: ["self-learning", "gotcha", "stripe"]
         ^namespace      ^type    ^domain
```

Retrieve with: `filter_tags: ["self-learning"]`
Browse with: `sage_memory_list(tags: ["self-learning"])`

### Type Tags

Second tag position — used for sub-categorization within a namespace:

| Namespace | Type tags |
|-----------|-----------|
| `self-learning` | `gotcha`, `correction`, `convention`, `api-drift`, `error-fix` |
| `ontology` | `entity`, `schema` |

### Lifecycle Tags

Used within a namespace to track entry state:

| Tag | Meaning |
|-----|---------|
| `verified` | Learning confirmed to be correct |
| `promoted` | Learning escalated to broader scope |
| `stale` | Entry may be outdated — needs review |

### Domain Tags

Remaining tag positions — project-specific vocabulary:

```
["self-learning", "gotcha", "stripe", "webhooks", "express"]
                             ^─────── domain tags ────────^
```

Use lowercase, specific terms. Technology names, module names, concepts.
These are what BM25 tag boosting matches against.

### Tag Count

3-6 tags per entry. 1 namespace + 1 type + 1-4 domain keywords.

Too few → hard to filter precisely.
Too many → dilutes tag boost signal.

## Content Conventions

### Knowledge (memory skill, no namespace tag)

Free-form prose explaining what AND why. Use the project's actual
vocabulary — class names, function names, domain terms.

```
Title: "Payment saga orchestration via PaymentOrchestrator with 3 services"
Content: "The billing service uses a saga pattern for multi-step payment
processing. PaymentOrchestrator coordinates between StripeGateway,
LedgerService, and NotificationService. Failures trigger compensating
transactions defined in saga_rollback_handlers."
Tags: ["billing", "saga", "payments", "architecture"]
```

### Ontology Entities (ontology skill)

JSON content with `id`, `type`, `properties`. Structured title with
type prefix and ID.

```
Title: "[Task:task_a1b2] Fix payment timeout in checkout flow"
Content: '{"id":"task_a1b2","type":"Task","properties":{"title":"Fix payment timeout","status":"open","priority":"high"}}'
Tags: ["ontology", "entity", "task", "billing", "checkout"]
```

**ID format:** `{type_prefix}_{8_hex}` where prefix is first 4 lowercase
chars of the type name.

```
Task    → task_a1b2c3d4
Person  → pers_e5f6a7b8
Project → proj_c9d0e1f2
Event   → even_a3b4c5d6
```

### Learnings (self-learning skill)

Four-part content structure with title prefix.

```
Title: "[LRN:gotcha] Stripe webhook requires raw body before JSON parsing"
Content: "What happened: Webhook signature verification failed with 400.
  Why: Express body parser replaced raw body with parsed JSON.
  What's correct: Use express.raw() for the webhook route.
  Prevention: Before implementing webhook handlers that verify signatures,
  check if the SDK requires the raw request body."
Tags: ["self-learning", "gotcha", "stripe", "webhooks"]
```

The prevention rule is the most important part — it's the behavioral
instruction that changes future agent behavior.

## Scope Rules

| Scope | When to use | Where stored |
|-------|-------------|-------------|
| `project` (default) | Codebase-specific knowledge | `.sage-memory/memory.db` at project root |
| `global` | Cross-project patterns, personal conventions | `~/.sage-memory/memory.db` |

**Rule of thumb:** If you'd want to know this in a different project
that uses the same library/tool, it should be global.

Search with `scope: "project"` hits both project and global DBs (project
results ranked higher). Search with `scope: "global"` hits global only.

## Edge Conventions

### Relation Types (defined by ontology skill)

| Relation | Typical usage |
|----------|--------------|
| `depends_on` | A requires B to function |
| `contains` | A has B as a component |
| `has_task` | Project → Task |
| `assigned_to` | Task → Person |
| `blocks` | Task blocks another Task (acyclic) |
| `part_of` | Task/Document belongs to Project |
| `applies_to` | Learning applies to an entity |
| `relates_to` | General association |

Custom relation types are allowed — the schema is open. Use lowercase
with underscores.

### Edge Properties

JSON properties on edges are optional and skill-defined:

```
sage_memory_link(
  source_id: "...", target_id: "...", relation: "depends_on",
  properties: {"confidence": 0.9, "discovered_by": "static_analysis"}
)
```

No enforced schema. Skills document what properties they use.

## Retrieval Patterns

### Keyword search (most common)

```
sage_memory_search(query: "billing saga pattern", limit: 5)
```

BM25 ranks by term match density. Title gets 10x weight, content 3x,
tags 1x. OR semantics — documents matching more terms rank higher.

### Namespaced search (skill isolation)

```
sage_memory_search(query: "stripe webhook", filter_tags: ["self-learning"], limit: 5)
```

Hard filter applied before BM25 ranking. Only entries matching ALL
filter tags are considered.

### Boosted search (preference without exclusion)

```
sage_memory_search(query: "payment processing", tags: ["billing"], limit: 5)
```

Entries tagged "billing" get +3% score boost (max 15% across all tag
matches). Non-billing entries still appear if BM25 ranks them highly.

### Browse (no query, tag filter only)

```
sage_memory_list(tags: ["self-learning", "gotcha"], limit: 20)
```

AND logic — all tags must match. Sorted by most recently updated.

### Graph traversal (structural)

```
sage_memory_graph(id: "<memory_id>", relation: "depends_on", direction: "outbound", depth: 2)
```

Returns connected memories within N hops. Cycle-safe. Direction:
outbound (source→target), inbound (target→source), both.

### Combined: search + expand

```
# Find by keyword
results = sage_memory_search(query: "payment service", filter_tags: ["ontology", "entity"])

# Expand neighborhood
sage_memory_graph(id: results[0].id, direction: "both", depth: 1)
```

Two calls — find what, then understand context.

## Deduplication

sage-memory deduplicates by SHA-256 content hash. Storing identical
content twice returns the existing entry's ID with a "Duplicate" message.

For near-duplicates (same topic, different wording), search before store
and update the existing entry instead of creating a new one.

## Quality Guidelines

**Specificity retrieves.** "Payment saga orchestration via
PaymentOrchestrator with 3 services" retrieves. "How payments work"
doesn't. Use the project's actual vocabulary.

**Title is the strongest signal.** BM25 gives title 10x weight. Put the
most important search terms in the title.

**Insights over facts.** Store what requires understanding, not what can
be re-read from source code.

**3-8 entries per task.** Quality over quantity. Noisy memories dilute
search results.

**Update, don't duplicate.** When understanding deepens, update the
existing entry via `sage_memory_update`. sage-memory catches exact
duplicates, but near-duplicates are your responsibility.

**Delete stale entries.** Outdated memories cause confident wrong answers.
If code has changed and a memory no longer reflects reality, delete it.
