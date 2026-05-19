"""Task 5 perf gate — `find_suggested_links` must stay ≤ 50ms p95 on
a 1K-memory synthetic corpus.

Per spec §`suggested_links.py`: ≤ 50ms p95 added to store(). This
test isolates the helper itself (no `store()` cost included).
"""

from __future__ import annotations

import random
import time

import pytest

from sage_memory import db as _db
from sage_memory.store import store
from sage_memory.suggested_links import find_suggested_links


P95_BUDGET_MS = 50.0
CORPUS_SIZE = 1000
N_RUNS = 20


@pytest.fixture(scope="module")
def thousand_memory_corpus(tmp_path_factory):
    """One-time setup: populate a fresh project with 1000 deterministic
    memories. `scope="module"` so the corpus builds once for the
    whole test file."""
    rng = random.Random(42)
    base_path = tmp_path_factory.mktemp("perf_corpus")
    (base_path / ".git").mkdir()
    _db.override_project_root(base_path)
    _db.close_all()

    topics = [
        "Stripe webhook signature verification with raw body parsing",
        "PaymentOrchestrator coordinates checkout flow with saga pattern",
        "PostgreSQL JSONB indexing for analytics query optimization",
        "Redis cache invalidation strategy for user profile updates",
        "Kubernetes pod autoscaling based on CPU and custom metrics",
        "ReactQuery cache management with optimistic UI updates",
        "TypeScript discriminated unions for state machine modeling",
        "GraphQL N+1 query problem solved with DataLoader batching",
        "JWT refresh token rotation with HttpOnly cookies and CSRF",
        "Postgres row-level security policies for multi-tenant SaaS",
    ]

    for i in range(CORPUS_SIZE):
        topic = topics[i % len(topics)]
        content = f"Entry {i}: {topic}. Notes vary {rng.randint(0, 10000)}."
        store(content=content, title=f"Topic {i % 50}", scope="project")

    yield base_path
    _db.close_all()
    _db.override_project_root(None)


def test_suggested_links_p95_under_50ms(thousand_memory_corpus):
    """p95 over 20 runs must stay ≤ 50ms with a 1K-memory corpus."""
    conn = _db.get_db("project")
    # Warm any caches (first run can be slow)
    find_suggested_links(
        conn,
        "Stripe webhook signature verification with raw body parsing.",
    )
    timings_ms = []
    rng = random.Random(7)
    queries = [
        "Stripe webhook signature verification with raw body parsing notes.",
        "PaymentOrchestrator coordinates the checkout flow saga pattern setup.",
        "PostgreSQL JSONB indexing for analytics query optimization details.",
        "Redis cache invalidation strategy for user profile updates here.",
        "Kubernetes pod autoscaling based on CPU and custom metrics review.",
    ]
    for _ in range(N_RUNS):
        q = rng.choice(queries) + f" run {rng.randint(0, 10000)}."
        t0 = time.perf_counter()
        find_suggested_links(conn, q)
        timings_ms.append((time.perf_counter() - t0) * 1000)

    timings_ms.sort()
    p95 = timings_ms[int(0.95 * len(timings_ms)) - 1]
    mean = sum(timings_ms) / len(timings_ms)
    print(
        f"\nsuggested_links p95={p95:.2f}ms  mean={mean:.2f}ms  "
        f"max={max(timings_ms):.2f}ms  n_runs={N_RUNS}  "
        f"corpus={CORPUS_SIZE}"
    )
    assert p95 <= P95_BUDGET_MS, (
        f"p95 {p95:.2f}ms exceeds {P95_BUDGET_MS}ms budget "
        f"(timings: {[round(t, 1) for t in timings_ms]})"
    )
