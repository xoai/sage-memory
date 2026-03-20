# Building Skills on sage-memory

Guide for developing skills that use sage-memory as their storage layer.

## What is a skill?

A skill is an instruction file (markdown) that teaches an LLM how to
behave in a specific domain. The LLM reads the skill and follows its
instructions, calling sage-memory tools to persist and retrieve data.

Skills are NOT code. They don't run. The LLM runs. Skills shape its
behavior — what to store, when to search, how to structure content.

## sage-memory Tools

Your skill has access to 7 tools:

| Tool | What it does |
|------|-------------|
| `sage_memory_store` | Store content with title, tags, scope |
| `sage_memory_search` | BM25 search with `tags` (boost) and `filter_tags` (hard filter) |
| `sage_memory_update` | Partial update by ID |
| `sage_memory_delete` | Delete by ID (CASCADE removes edges) |
| `sage_memory_list` | Browse with tag filtering (AND logic) |
| `sage_memory_link` | Create/delete typed edges between memories |
| `sage_memory_graph` | Traverse edges (multi-hop, cycle-safe) |

## Namespace Isolation

If your skill stores entries that should be distinguishable from regular
memories, use a namespace tag:

```
tags: ["my-skill-name", "domain-tag", ...]
```

To retrieve only your skill's entries:

```
sage_memory_search(query: "...", filter_tags: ["my-skill-name"])
sage_memory_list(tags: ["my-skill-name"])
```

`filter_tags` applies a hard WHERE filter — only entries with ALL
specified tags are returned. `tags` on search is a soft boost —
prefers but doesn't exclude.

**Convention:** The first tag is your skill's namespace. Remaining tags
are domain-specific. This pattern is used by the self-learning skill
(`self-learning`) and ontology skill (`ontology`).

## Content Structure

sage-memory uses FTS5 BM25 for keyword search. Content retrieves well
when it uses consistent, specific vocabulary.

**Titles:** 5-15 words, specific and descriptive. Include the most
important search terms. The title gets 10x BM25 weight — it's the
strongest retrieval signal.

**Content:** Detailed explanation. Use the project's actual class names,
function names, domain concepts. BM25 matches on exact terms, so content
that says "PaymentOrchestrator" will be found by queries containing
"PaymentOrchestrator."

**Tags:** 2-6 lowercase keywords. Technology, domain area, skill-specific
type. Used for filtering (via `filter_tags` and `list` tags) and
soft boosting.

## Using Graph Features

If your skill models relationships between entities, use `sage_memory_link`
to create typed edges and `sage_memory_graph` to traverse them.

### When to use links vs tags

**Use `sage_memory_link` when:**
- You need multi-hop traversal ("what depends on X, and what depends on those?")
- You need CASCADE cleanup (deleting an entity removes all its relationships)
- The relationship has properties (confidence, notes, timestamp)
- You need directionality (A depends on B ≠ B depends on A)

**Use tags when:**
- Simple categorization ("this entry is about billing")
- Flat filtering ("show me all entries tagged 'security'")
- Cross-skill linking ("this learning relates to entity X" — add `edge:X` tag)

Both approaches coexist. Tags are lightweight. Links are structural.

### Creating entities with relationships

```
# 1. Store entity A
result_a = sage_memory_store(content: "...", title: "Entity A", tags: ["my-skill", "entity"])

# 2. Store entity B
result_b = sage_memory_store(content: "...", title: "Entity B", tags: ["my-skill", "entity"])

# 3. Link them
sage_memory_link(source_id: result_a.id, target_id: result_b.id, relation: "depends_on")
```

### Traversing

```
sage_memory_graph(id: "<entity_id>", relation: "depends_on", direction: "outbound", depth: 2)
```

Returns all memories reachable within 2 hops via `depends_on` edges.
Cycle-safe — won't loop on circular dependencies.

## Skill File Structure

```
skills/
└── my-skill/
    ├── SKILL.md              ← Main instruction file (required)
    └── references/           ← Supporting docs (optional)
        ├── examples.md
        └── conventions.md
```

### SKILL.md Format

```markdown
---
name: my-skill
description: >
  One paragraph explaining when this skill activates. Include trigger
  phrases the LLM should recognize.
version: "1.0.0"
type: process
---

# My Skill

What this skill does in 1-2 sentences.

## Tools Used

List the sage-memory tools this skill calls, with purpose.

## How It Works

Step-by-step instructions for the LLM. Include exact tool call syntax.
Be literal — the LLM follows these instructions as-is.

## Storage Conventions

What you store, how you tag it, how content is structured.

## References

Links to supporting docs in references/.
```

### Key Principles

**Be literal.** Write exact tool names: `sage_memory_store`, not "store
in memory." LLMs dispatch to what you name.

**Show examples.** Include complete tool call examples with realistic
content. The LLM mirrors the patterns you show.

**Define conventions.** Tag naming, content structure, title format —
document everything that affects retrieval quality.

**Handle fallback.** What happens when sage-memory is unavailable?
Degrade gracefully — never block work.

## Skill Contract (optional)

If your skill interacts with the ontology, declare what it reads and writes:

```yaml
ontology:
  reads: [Task, Project]
  writes: [MyCustomType]
  relations: [my_custom_relation]
```

This helps other skill authors understand data dependencies.

## Testing

Use the test harness pattern from sage-memory's test suite:

1. Set up a temp project directory
2. Store seed data via `sage_memory_store`
3. Create relationships via `sage_memory_link`
4. Run queries and verify results
5. Check tag isolation, recall, precision
6. Measure latency

See `tests/test_all.py` for examples covering all 7 tools.
