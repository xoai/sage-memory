"""T7 — semantic_dedup performance budget.

Skipped by default (pyproject.toml: `addopts = "-m 'not perf'"`).
Run explicitly:
    ~/.venvs/sage-memory/bin/python -m pytest -m perf tests/test_semantic_dedup_perf.py -v

Spec budget: p99 ≤ 50ms across BOTH a cold-path (random vectors,
threshold never fires) and a hot-path (clustered vectors,
threshold fires every query) distribution. Random-only seeding
would understate cost — the threshold-positive branch never runs.

Methodology:
- 8000 random unit vectors → cold queries draw from random unit
  vectors (cosine ≈ 0 to every neighbor; threshold never fires).
- 2000 cluster-noise vectors → 50 centroids × 40 each; hot queries
  draw from centroid neighborhoods (intra-cluster cosine > 0.95).
- 50 cold + 50 hot probes; report p50/p95/p99 and assert overall
  p99 ≤ 50ms.
"""

from __future__ import annotations

import math
import random
import time

import pytest

from sage_memory.db import _open, get_project_db_path
from sage_memory.embedder import EMBEDDING_DIM, serialize_vec
from sage_memory.semantic_dedup import find_near_duplicate


_PERF_BUDGET_MS = 50.0   # p99 ≤ 50ms across hot + cold
_N_RANDOM = 8000
_N_CLUSTER_CENTROIDS = 50
_N_PER_CLUSTER = 40
_N_PROBES_PER_DIST = 50
# In 384-dim, Gaussian noise σ per coordinate gives total
# offset variance ≈ d*σ² → cos(orig, noisy) ≈ 1/sqrt(1 + dσ²).
# σ=0.005 → cos ≈ 0.995 to centroid; pairwise cluster cosine ≈ 0.99.
_CLUSTER_NOISE_SIGMA = 0.005


def _unit(seed: int) -> list[float]:
    r = random.Random(seed)
    raw = [r.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    n = math.sqrt(sum(x * x for x in raw))
    return [x / n for x in raw]


def _add_noise(base: list[float], seed: int, sigma: float) -> list[float]:
    r = random.Random(seed)
    noisy = [b + r.gauss(0, sigma) for b in base]
    n = math.sqrt(sum(x * x for x in noisy))
    return [x / n for x in noisy]


@pytest.mark.perf
def test_find_near_duplicate_p99_under_50ms_on_10k_db(tmp_path):
    db = _open(get_project_db_path(tmp_path))
    now = time.time()

    # ── Seed cold: 8000 random unit vectors ───────────────────────
    for i in range(_N_RANDOM):
        v = _unit(i)
        mid = f"rand_{i}"
        db.execute(
            "INSERT INTO memories (id, title, content, tags, content_hash, "
            "embedded, status, created_at, updated_at, accessed_at, access_count) "
            "VALUES (?, ?, 'c', '[]', ?, 1, 'active', ?, ?, ?, 0)",
            (mid, f"t{i}", f"h{mid}", now, now, now),
        )
        db.execute(
            "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
            (mid, serialize_vec(v)),
        )

    # ── Seed hot: 50 centroids × 40 vectors ───────────────────────
    centroids = [_unit(10_000 + c) for c in range(_N_CLUSTER_CENTROIDS)]
    for c_idx, centroid in enumerate(centroids):
        for j in range(_N_PER_CLUSTER):
            v = _add_noise(centroid, seed=20_000 + c_idx * 100 + j, sigma=_CLUSTER_NOISE_SIGMA)
            mid = f"clust_{c_idx}_{j}"
            db.execute(
                "INSERT INTO memories (id, title, content, tags, content_hash, "
                "embedded, status, created_at, updated_at, accessed_at, access_count) "
                "VALUES (?, ?, 'c', '[]', ?, 1, 'active', ?, ?, ?, 0)",
                (mid, f"t{mid}", f"h{mid}", now, now, now),
            )
            db.execute(
                "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
                (mid, serialize_vec(v)),
            )
    db.commit()

    # Sanity: total seed size should be ~10k.
    total = db.execute("SELECT COUNT(*) AS n FROM memories_vec").fetchone()["n"]
    assert total == _N_RANDOM + _N_CLUSTER_CENTROIDS * _N_PER_CLUSTER

    # ── Cold probes: random unit vectors (threshold never fires) ──
    cold_times = []
    for q in range(_N_PROBES_PER_DIST):
        probe = _unit(50_000 + q)
        t0 = time.perf_counter()
        result = find_near_duplicate(db, embedding=probe)
        cold_times.append((time.perf_counter() - t0) * 1000.0)
        # Random probes against random+cluster seed: probability of
        # any neighbor being > 0.95 cosine is effectively zero.
        # We don't assert result is None (a stray near-miss is fine);
        # we just measure cost.

    # ── Hot probes: noisy centroid neighbors (threshold fires) ────
    hot_times = []
    hot_hit_count = 0
    for c_idx in range(_N_PROBES_PER_DIST):
        centroid = centroids[c_idx % _N_CLUSTER_CENTROIDS]
        probe = _add_noise(
            centroid, seed=70_000 + c_idx, sigma=_CLUSTER_NOISE_SIGMA,
        )
        t0 = time.perf_counter()
        result = find_near_duplicate(db, embedding=probe)
        hot_times.append((time.perf_counter() - t0) * 1000.0)
        if result is not None:
            hot_hit_count += 1

    # Hot path actually exercised the threshold-positive branch
    # (otherwise we'd just be re-measuring cold).
    assert hot_hit_count >= _N_PROBES_PER_DIST // 2, (
        f"hot-path probes only triggered threshold {hot_hit_count}/"
        f"{_N_PROBES_PER_DIST} times; cluster noise sigma may be too "
        f"large or k=5 not catching the seeded neighbor"
    )

    # ── Report + assert overall p99 ≤ 50ms ───────────────────────
    all_times = sorted(cold_times + hot_times)
    n = len(all_times)
    p50 = all_times[int(n * 0.50)]
    p95 = all_times[int(n * 0.95)]
    p99 = all_times[int(n * 0.99) if n > 100 else n - 1]
    print(
        f"\nsemantic_dedup perf (10k DB, {n} probes): "
        f"p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms "
        f"max={max(all_times):.2f}ms "
        f"(cold p99={sorted(cold_times)[-1]:.2f} hot p99={sorted(hot_times)[-1]:.2f}, "
        f"hot threshold-hits={hot_hit_count}/{_N_PROBES_PER_DIST})"
    )
    assert p99 <= _PERF_BUDGET_MS, (
        f"p99 {p99:.2f}ms exceeds budget {_PERF_BUDGET_MS}ms"
    )
    db.close()
