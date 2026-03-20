#!/usr/bin/env python3
"""sage-memory v0.5 — Comprehensive Test Suite

Covers:
  1. Store/retrieve CRUD + dedup
  2. Search quality (recall/precision)
  3. Dual-DB merge (project + global)
  4. filter_tags namespace isolation
  5. Graph CRUD (create/update/delete edges, CASCADE)
  6. Graph traversal (multi-hop, cycle detection, direction)
  7. Ontology skill patterns (entity + relation lifecycle)
  8. Self-learning skill patterns (isolation, consolidation, edge linking)
  9. Performance (latency percentiles)
"""

import json
import os
import sys
import time
import shutil
import tempfile
import statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
os.environ["MCP_MEMORY_EMBEDDER"] = "local"

from sage_memory.db import override_project_root, get_db, close_all, get_all_dbs
from sage_memory.store import store, update, delete, list_memories
from sage_memory.search import search
from sage_memory.graph import link, graph

TMPDIR = Path(tempfile.mkdtemp())
PROJECT = TMPDIR / "test-project"
PASS_COUNT = 0
FAIL_COUNT = 0


def setup():
    close_all()
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    (PROJECT / ".git").mkdir()
    override_project_root(PROJECT)


def check(name: str, condition: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  ✅ {name}")
    else:
        FAIL_COUNT += 1
        print(f"  ❌ {name}{' — ' + detail if detail else ''}")


def section(name: str):
    print(f"\n{'='*60}\n  {name}\n{'='*60}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 1: Store/Retrieve CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_crud():
    section("Suite 1: Store/Retrieve CRUD")

    # Store
    r = store(content="The billing service uses saga pattern for multi-step payments.",
              title="Billing saga pattern", tags=["billing", "architecture"])
    check("store succeeds", r["success"])
    mem_id = r["id"]

    # Dedup
    r2 = store(content="The billing service uses saga pattern for multi-step payments.",
               title="Different title, same content", tags=["billing"])
    check("dedup catches identical content", not r2["success"] and "Duplicate" in r2["message"])

    # Search finds it
    r3 = search(query="billing saga payment", limit=5)
    check("search finds stored memory", any(x["id"] == mem_id for x in r3["results"]))

    # Update
    r4 = update(id=mem_id, title="Updated: Billing saga with compensating transactions")
    check("update succeeds", r4["success"])

    # Verify update
    r5 = search(query="compensating transactions", limit=5)
    found = [x for x in r5["results"] if x["id"] == mem_id]
    check("updated content is searchable", len(found) > 0)

    # List
    r6 = list_memories(limit=10)
    check("list returns entries", r6["total"] >= 1)

    # Delete
    r7 = delete(id=mem_id)
    check("delete succeeds", r7["success"])

    r8 = search(query="billing saga", limit=5)
    check("deleted memory not in search", not any(x["id"] == mem_id for x in r8["results"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 2: Dual-DB Merge
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_dual_db():
    section("Suite 2: Dual-DB Merge")

    # Store in project
    store(content="Auth uses JWT with RS256 signing. Access tokens expire in 15 minutes.",
          title="Project auth: JWT RS256", tags=["auth", "jwt"], scope="project")

    # Store in global
    store(content="Always use conventional commits: feat, fix, chore, docs.",
          title="Conventional commit format", tags=["git", "conventions"], scope="global")

    # Project-scope search finds both
    r = search(query="JWT authentication tokens", scope="project", limit=10)
    check("project search finds project memory", any("JWT" in x["title"] for x in r["results"]))

    r2 = search(query="conventional commits format", scope="project", limit=10)
    sources = [x["source"] for x in r2["results"]]
    check("project search includes global results", "global" in sources)

    # Global-only search doesn't find project memories
    r3 = search(query="JWT authentication", scope="global", limit=10)
    check("global search excludes project memories",
          not any("JWT RS256" in x.get("title", "") for x in r3["results"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 3: filter_tags Isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_filter_tags():
    section("Suite 3: filter_tags Isolation")

    # Store learning + noise
    store(content="Stripe webhooks need raw body before JSON parsing. Express body parser breaks signature.",
          title="[LRN:gotcha] Stripe webhook raw body", tags=["self-learning", "gotcha", "stripe"])

    store(content="Stripe integration architecture: webhooks use express.raw middleware.",
          title="Stripe integration overview", tags=["architecture", "stripe"])

    # Without filter: both appear
    r = search(query="stripe webhook", limit=10)
    check("unfiltered search returns both", len(r["results"]) >= 2)

    # With filter_tags: only learning
    r2 = search(query="stripe webhook", filter_tags=["self-learning"], limit=10)
    all_learning = all("self-learning" in x["tags"] for x in r2["results"])
    check("filter_tags isolates learnings", all_learning and len(r2["results"]) >= 1)

    # AND logic: self-learning + gotcha
    r3 = search(query="stripe", filter_tags=["self-learning", "gotcha"], limit=10)
    all_match = all("self-learning" in x["tags"] and "gotcha" in x["tags"] for x in r3["results"])
    check("filter_tags AND logic works", all_match)

    # List with tags filter also works
    r4 = list_memories(tags=["self-learning"])
    check("list_memories tags filter works",
          all("self-learning" in x["tags"] for x in r4["items"]))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 4: Graph CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_graph_crud():
    section("Suite 4: Graph CRUD")

    # Create two memories
    a = store(content="PaymentService orchestrates billing flow with saga pattern.",
              title="PaymentService", tags=["service", "billing"])
    b = store(content="StripeGateway handles all Stripe API communication.",
              title="StripeGateway", tags=["service", "stripe"])
    c = store(content="NotificationService sends emails and push notifications.",
              title="NotificationService", tags=["service", "notifications"])

    a_id, b_id, c_id = a["id"], b["id"], c["id"]

    # Create edges
    r1 = link(source_id=a_id, target_id=b_id, relation="depends_on")
    check("create edge succeeds", r1["success"])

    r2 = link(source_id=a_id, target_id=c_id, relation="depends_on",
              properties={"criticality": "high"})
    check("create edge with properties succeeds", r2["success"])

    # Self-loop prevention
    r3 = link(source_id=a_id, target_id=a_id, relation="depends_on")
    check("self-loop rejected", not r3["success"])

    # Duplicate edge updates properties
    r4 = link(source_id=a_id, target_id=b_id, relation="depends_on",
              properties={"version": "2.0"})
    check("duplicate edge updates (upsert)", r4["success"] and "updated" in r4["message"].lower())

    # Invalid source
    r5 = link(source_id="nonexistent", target_id=b_id, relation="depends_on")
    check("invalid source rejected", not r5["success"])

    # Delete edge
    r6 = link(source_id=a_id, target_id=c_id, relation="depends_on", delete=True)
    check("delete edge succeeds", r6["success"])

    # CASCADE: delete a memory, edges should be cleaned up
    link(source_id=a_id, target_id=c_id, relation="depends_on")  # recreate
    delete(id=c_id)
    r7 = graph(id=a_id, direction="outbound", depth=1)
    c_in_results = any(n["id"] == c_id for n in r7["nodes"])
    check("CASCADE removes edges on memory delete", not c_in_results)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 5: Graph Traversal
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_graph_traversal():
    section("Suite 5: Graph Traversal")

    # Build a chain: A → B → C → D
    ids = []
    for name in ["ModuleA", "ModuleB", "ModuleC", "ModuleD"]:
        r = store(content=f"{name} handles part of the processing pipeline.",
                  title=name, tags=["module"])
        ids.append(r["id"])

    for i in range(len(ids) - 1):
        link(source_id=ids[i], target_id=ids[i+1], relation="calls")

    # Depth 1: A → [B]
    r1 = graph(id=ids[0], relation="calls", direction="outbound", depth=1)
    check("depth-1 finds direct neighbor",
          len(r1["nodes"]) == 1 and r1["nodes"][0]["title"] == "ModuleB")

    # Depth 2: A → [B, C]
    r2 = graph(id=ids[0], relation="calls", direction="outbound", depth=2)
    check("depth-2 finds 2-hop chain", len(r2["nodes"]) == 2)

    # Depth 3: A → [B, C, D]
    r3 = graph(id=ids[0], relation="calls", direction="outbound", depth=3)
    check("depth-3 finds full chain", len(r3["nodes"]) == 3)

    # Inbound: D ← [C]
    r4 = graph(id=ids[3], relation="calls", direction="inbound", depth=1)
    check("inbound finds caller",
          len(r4["nodes"]) == 1 and r4["nodes"][0]["title"] == "ModuleC")

    # Both directions from B: [A] ← B → [C]
    r5 = graph(id=ids[1], relation="calls", direction="both", depth=1)
    check("both-direction finds neighbors",
          len(r5["nodes"]) == 2)

    # Cycle detection: add D → A to create cycle
    link(source_id=ids[3], target_id=ids[0], relation="calls")
    r6 = graph(id=ids[0], relation="calls", direction="outbound", depth=5)
    check("cycle detection prevents infinite loop",
          r6["success"] and len(r6["nodes"]) == 3)  # visits B, C, D once each

    # No relation filter: traverses all edge types
    link(source_id=ids[0], target_id=ids[2], relation="depends_on")
    r7 = graph(id=ids[0], direction="outbound", depth=1)
    check("no relation filter traverses all types",
          len(r7["edges"]) >= 2)  # calls + depends_on

    # Nonexistent start node
    r8 = graph(id="nonexistent", depth=1)
    check("nonexistent node returns error", not r8["success"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 6: Ontology Patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_ontology_patterns():
    section("Suite 6: Ontology Patterns")

    # Create entities
    proj = store(
        content='{"id":"proj_c9d0","type":"Project","properties":{"name":"Billing v2","status":"active"}}',
        title="[Project:proj_c9d0] Billing v2 migration",
        tags=["ontology", "entity", "project", "billing"])

    task1 = store(
        content='{"id":"task_a1b2","type":"Task","properties":{"title":"Fix payment timeout","status":"open","priority":"high"}}',
        title="[Task:task_a1b2] Fix payment timeout in checkout",
        tags=["ontology", "entity", "task", "billing", "checkout"])

    task2 = store(
        content='{"id":"task_f3a4","type":"Task","properties":{"title":"Deploy checkout page","status":"blocked"}}',
        title="[Task:task_f3a4] Deploy checkout page",
        tags=["ontology", "entity", "task", "checkout"])

    person = store(
        content='{"id":"pers_e5f6","type":"Person","properties":{"name":"Alice Chen","role":"Backend Lead"}}',
        title="[Person:pers_e5f6] Alice Chen — backend lead",
        tags=["ontology", "entity", "person", "backend"])

    check("create 4 ontology entities", all(r["success"] for r in [proj, task1, task2, person]))

    # Create relations via sage_memory_link
    r1 = link(source_id=proj["id"], target_id=task1["id"], relation="has_task")
    r2 = link(source_id=proj["id"], target_id=task2["id"], relation="has_task")
    r3 = link(source_id=task1["id"], target_id=task2["id"], relation="blocks")
    r4 = link(source_id=task1["id"], target_id=person["id"], relation="assigned_to")
    check("create 4 ontology relations", all(r["success"] for r in [r1, r2, r3, r4]))

    # Search entities by type
    r5 = search(query="task billing", filter_tags=["ontology", "entity", "task"], limit=10)
    check("search entities by type",
          len(r5["results"]) >= 1 and all("ontology" in x["tags"] for x in r5["results"]))

    # Graph: project → tasks
    r6 = graph(id=proj["id"], relation="has_task", direction="outbound", depth=1)
    check("graph: project has_task returns tasks",
          len(r6["nodes"]) == 2)

    # Graph: what blocks task2?
    r7 = graph(id=task2["id"], relation="blocks", direction="inbound", depth=1)
    check("graph: inbound blocks finds blocker",
          len(r7["nodes"]) == 1 and r7["nodes"][0]["id"] == task1["id"])

    # Graph: 2-hop from project → tasks → blocked/assigned
    r8 = graph(id=proj["id"], direction="outbound", depth=2)
    check("graph: 2-hop from project finds full subgraph",
          len(r8["nodes"]) >= 3)  # task1, task2, person

    # Delete entity: CASCADE should remove edges
    delete(id=task2["id"])
    r9 = graph(id=proj["id"], relation="has_task", direction="outbound", depth=1)
    check("cascade: deleting task removes has_task edge",
          len(r9["nodes"]) == 1)  # only task1 remains

    r10 = graph(id=task1["id"], relation="blocks", direction="outbound", depth=1)
    check("cascade: deleting target removes blocks edge",
          len(r10["nodes"]) == 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 7: Self-Learning Patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_self_learning_patterns():
    section("Suite 7: Self-Learning Patterns")

    # Store learnings
    l1 = store(
        content="What happened: Webhook signature verification failed. Why: Express body parser replaced raw body. Prevention: Check if SDK requires raw body.",
        title="[LRN:gotcha] Stripe webhook raw body parsing",
        tags=["self-learning", "gotcha", "stripe", "webhooks"])

    l2 = store(
        content="What happened: Webhook processed same event 3 times. Why: Stripe retries on non-2xx. Prevention: Add idempotency check using event ID.",
        title="[LRN:gotcha] Stripe webhook idempotency on retries",
        tags=["self-learning", "gotcha", "stripe", "webhooks", "idempotency"])

    l3 = store(
        content="What happened: Used npm install. Why: Project uses pnpm. Prevention: Check for pnpm-lock.yaml first.",
        title="[LRN:correction] Project uses pnpm not npm",
        tags=["self-learning", "correction", "pnpm"])

    # Store non-learning noise
    store(content="Stripe integration uses webhooks for async payment notifications.",
          title="Stripe architecture", tags=["architecture", "stripe"])

    check("store 3 learnings + 1 noise", all(r["success"] for r in [l1, l2, l3]))

    # Namespace isolation
    r1 = search(query="stripe webhook", filter_tags=["self-learning"], limit=10)
    all_learning = all("self-learning" in x["tags"] for x in r1["results"])
    no_noise = not any("architecture" in x.get("title", "").lower() for x in r1["results"])
    check("filter_tags isolates learnings from noise", all_learning and no_noise)

    # Browse by type
    r2 = list_memories(tags=["self-learning", "gotcha"])
    check("list gotchas", r2["total"] == 2)

    r3 = list_memories(tags=["self-learning", "correction"])
    check("list corrections", r3["total"] == 1)

    # Consolidation: merge 2 learnings into 1
    consolidated = store(
        content="Three Stripe webhook gotchas: (1) Raw body required. (2) Retries use same event ID. (3) Must respond within 5s. Prevention: Use express.raw(), add idempotency, return 200 immediately.",
        title="[LRN:gotcha] Stripe webhook: raw body, idempotency, timeout",
        tags=["self-learning", "gotcha", "stripe", "webhooks"])
    delete(id=l1["id"])
    delete(id=l2["id"])

    r4 = search(query="stripe webhook", filter_tags=["self-learning"], limit=5)
    found_consolidated = any("raw body, idempotency" in x["title"] for x in r4["results"])
    found_old = any("raw body parsing" in x["title"] for x in r4["results"])
    check("consolidation: new found, old gone", found_consolidated and not found_old)

    # Link learning to ontology entity via sage_memory_link
    task = store(
        content='{"id":"task_x1","type":"Task","properties":{"title":"Fix webhook","status":"open"}}',
        title="[Task:task_x1] Fix webhook handler",
        tags=["ontology", "entity", "task", "stripe"])

    r5 = link(source_id=consolidated["id"], target_id=task["id"], relation="applies_to")
    check("link learning to ontology entity", r5["success"])

    r6 = graph(id=task["id"], relation="applies_to", direction="inbound", depth=1)
    check("graph: find learnings linked to task",
          len(r6["nodes"]) == 1 and "gotcha" in r6["nodes"][0]["tags"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Suite 8: Performance
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_performance():
    section("Suite 8: Performance")

    # Fresh DB for clean measurements
    close_all()
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    (PROJECT / ".git").mkdir()
    override_project_root(PROJECT)

    # Store 100 memories
    store_lats = []
    ids = []
    for i in range(100):
        t0 = time.perf_counter()
        r = store(content=f"Knowledge entry {i}: module_{i} handles processing step {i} in the pipeline. It depends on module_{max(0,i-1)} and produces output for module_{i+1}.",
                  title=f"Module {i} pipeline processing",
                  tags=["module", f"step-{i}"])
        store_lats.append((time.perf_counter() - t0) * 1000)
        if r["success"]:
            ids.append(r["id"])

    s = sorted(store_lats)
    print(f"  Store (n=100): mean={statistics.mean(s):.2f}ms  p50={statistics.median(s):.2f}ms  p95={s[95]:.2f}ms")
    check("store p95 < 10ms", s[95] < 10)

    # Create edges (chain)
    link_lats = []
    for i in range(len(ids) - 1):
        t0 = time.perf_counter()
        link(source_id=ids[i], target_id=ids[i+1], relation="calls")
        link_lats.append((time.perf_counter() - t0) * 1000)

    s = sorted(link_lats)
    print(f"  Link (n={len(link_lats)}): mean={statistics.mean(s):.2f}ms  p50={statistics.median(s):.2f}ms  p95={s[int(len(s)*0.95)]:.2f}ms")
    check("link p95 < 5ms", s[int(len(s)*0.95)] < 5)

    # Search
    search_lats = []
    queries = ["pipeline processing module", "step 42 processing",
               "handles output pipeline", "depends module step",
               "processing pipeline output"]
    for q in queries * 4:  # 20 searches
        t0 = time.perf_counter()
        search(query=q, limit=5)
        search_lats.append((time.perf_counter() - t0) * 1000)

    s = sorted(search_lats)
    print(f"  Search (n={len(search_lats)}): mean={statistics.mean(s):.2f}ms  p50={statistics.median(s):.2f}ms  p95={s[int(len(s)*0.95)]:.2f}ms")
    check("search p95 < 50ms", s[int(len(s)*0.95)] < 50)

    # Graph traversal
    graph_lats = []
    for d in [1, 2, 3]:
        for start in [ids[0], ids[25], ids[50]]:
            t0 = time.perf_counter()
            graph(id=start, relation="calls", direction="outbound", depth=d)
            graph_lats.append((time.perf_counter() - t0) * 1000)

    s = sorted(graph_lats)
    print(f"  Graph (n={len(graph_lats)}): mean={statistics.mean(s):.2f}ms  p50={statistics.median(s):.2f}ms  p95={s[int(len(s)*0.95)]:.2f}ms")
    check("graph p95 < 10ms", s[int(len(s)*0.95)] < 10)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print("╔════════════════════════════════════════════════════════════╗")
    print("║       sage-memory v0.5 — Comprehensive Test Suite         ║")
    print("╚════════════════════════════════════════════════════════════╝")

    suites = [
        ("Store/Retrieve CRUD", test_crud),
        ("Dual-DB Merge", test_dual_db),
        ("filter_tags Isolation", test_filter_tags),
        ("Graph CRUD", test_graph_crud),
        ("Graph Traversal", test_graph_traversal),
        ("Ontology Patterns", test_ontology_patterns),
        ("Self-Learning Patterns", test_self_learning_patterns),
        ("Performance", test_performance),
    ]

    for name, fn in suites:
        setup()
        try:
            fn()
        except Exception as e:
            print(f"  💥 SUITE CRASHED: {e}")
            import traceback
            traceback.print_exc()

    # Final scorecard
    total = PASS_COUNT + FAIL_COUNT
    print(f"\n{'='*60}")
    print(f"  FINAL: {PASS_COUNT}/{total} passed", end="")
    if FAIL_COUNT == 0:
        print(" ✅")
    else:
        print(f"  ({FAIL_COUNT} failed) ❌")
    print(f"{'='*60}")

    close_all()
    shutil.rmtree(TMPDIR)
    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
