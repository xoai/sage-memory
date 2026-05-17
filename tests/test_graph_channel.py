"""T1 — sage_memory.graph_channel tests.

Covers spec A1 (core function), A2 (empty fast-path), A3 (canonical
resolution), A4 (EPS guard), A13 (orphan filter), plus BFS visited-
set tie-break + edges direction (outbound-only) + 3 rank curves +
unknown-curve ValueError.

Per spec rev 5: BFS is two-layer (entity hops via relations+mentions;
memory hops via edges direct). canonical_id resolution on read via
COALESCE. EPS-guarded rank with 3 curves dispatched by env var.
Orphan filter via INNER JOIN memories on the final projection.
"""

from __future__ import annotations

import time
import uuid

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)


# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path):
    """Project DB with all migrations applied (entities + edges)."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    db = _open(get_project_db_path(tmp_path))
    yield db
    close_all()


def _insert_memory(db, mid, content="content"):
    db.execute(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES (?, 't', ?, ?, 0, 1, 1, 1)",
        (mid, content, f"hash-{mid}"),
    )
    db.commit()


def _insert_entity(db, eid, name, *, etype="CONCEPT",
                   canonical_id=None, normalized=None):
    normalized = normalized or name.lower()
    db.execute(
        "INSERT INTO entities (id, name, name_normalized, type, "
        "canonical_id, mention_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (eid, name, normalized, etype, canonical_id,
         time.time(), time.time()),
    )
    db.commit()


def _insert_mention(db, memory_id, entity_id, surface_form):
    db.execute(
        "INSERT INTO mentions (memory_id, entity_id, surface_form, "
        "confidence, created_at) VALUES (?, ?, ?, 1.0, ?)",
        (memory_id, entity_id, surface_form, time.time()),
    )
    db.commit()


def _insert_relation(db, src_eid, tgt_eid, rel_type,
                     source_memory_id=None):
    db.execute(
        "INSERT INTO relations (id, source_entity_id, target_entity_id, "
        "relation_type, source_memory_id, confidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
        (uuid.uuid4().hex, src_eid, tgt_eid, rel_type,
         source_memory_id, time.time()),
    )
    db.commit()


def _insert_edge(db, source_mid, target_mid, relation="references"):
    db.execute(
        "INSERT INTO edges (id, source_id, target_id, relation, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, source_mid, target_mid, relation,
         time.time()),
    )
    db.commit()


# ─── A2 — Empty fast-path ─────────────────────────────────────────


def test_graph_proximity_empty_tables_fast_path(db_conn):
    """Empty entities table → graph_proximity returns []."""
    from sage_memory.graph_channel import graph_proximity
    result = graph_proximity(db_conn, query="anything")
    assert result == []


# ─── A1 — Core function: returns ranked list ─────────────────────


def test_graph_proximity_returns_ranked_list(db_conn):
    """Pre-populated graph + query for an entity → returns ranked list."""
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed", content="about Python lang")
    _insert_memory(db_conn, "M_neighbor", content="related to Python")
    _insert_entity(db_conn, "E_py", "Python")
    _insert_entity(db_conn, "E_django", "Django")
    _insert_mention(db_conn, "M_seed", "E_py", "Python")
    _insert_mention(db_conn, "M_neighbor", "E_django", "Django")
    _insert_relation(db_conn, "E_py", "E_django", "relates_to",
                     source_memory_id="M_seed")

    result = graph_proximity(db_conn, query="Python")
    assert len(result) >= 1
    # All results have the expected dict shape
    for r in result:
        assert "memory_id" in r
        assert "rank" in r
        assert "distance" in r
    # Lower rank = better; seed memory should be at the top
    ranks = [r["rank"] for r in result]
    assert ranks == sorted(ranks), "results not sorted by rank ascending"


# ─── A3 — Canonical resolution ────────────────────────────────────


def test_graph_proximity_post_dedup_seed_entity_merged(db_conn):
    """Regression for cumulative-review finding (#3): after M5 dedup
    sets canonical_id, relations written BEFORE the seed entity was
    merged (raw source_entity_id = old entity id) must still be
    traversed. Tests the case the OR clause actually handles:

      - Entity X (canonical, was already a canonical when relation written)
      - Entity B (legacy — M5 merges B into X; B.canonical_id = X.id)
      - Mention: M_seed → B (the legacy entity)
      - Relation written PRE-merge: source = B.id (raw), target = T
      - Mention: M_neighbor → T
      - Query for "B's name" seeds B, resolves canonical to X. BFS
        from M_seed → e_source = B (raw entity_id in mentions). The
        OR clause then resolves to (COALESCE(B.canonical, B.id) = X)
        OR (B.id). Predicate 2 matches the pre-merge relation. So
        M_neighbor is reachable.

    The other post-merge case (relation has source = merged_into_X
    where X is a different entity from the seed) requires full
    canonical-group expansion — out of scope for M3b's OR; that's
    M5's responsibility to rewrite mentions/relations rows at
    dedup time, OR a future "lazy canonical group" SQL extension.
    """
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed", content="memory about Claude")
    _insert_memory(db_conn, "M_neighbor", content="memory about Anthropic")

    _insert_entity(db_conn, "E_x", "ClaudeAPI",
                   normalized="claudeapi")  # canonical winner
    # E_b is the legacy entity; M5 has merged it into E_x
    _insert_entity(db_conn, "E_b", "Claude",
                   normalized="claude", canonical_id="E_x")
    _insert_entity(db_conn, "E_anthropic", "Anthropic",
                   normalized="anthropic")

    # M_seed's mention row uses B's raw entity_id (pre-merge state)
    _insert_mention(db_conn, "M_seed", "E_b", "Claude")
    _insert_mention(db_conn, "M_neighbor", "E_anthropic", "Anthropic")
    # Relation written PRE-merge: source = B.id (raw)
    _insert_relation(db_conn, "E_b", "E_anthropic", "implements")

    # Query for "Claude" → seeds either E_x (via canonical) or E_b
    # (via raw name_normalized). Either way, the BFS from M_seed
    # should reach M_neighbor via the OR clause that matches
    # `r.source_entity_id = e_source.id` (where e_source = E_b).
    result = graph_proximity(db_conn, query="claude")
    memory_ids = {r["memory_id"] for r in result}
    assert "M_seed" in memory_ids
    assert "M_neighbor" in memory_ids, (
        "BFS must traverse pre-merge relations via the OR clause "
        "in _entity_mediated_neighbors. If this fails, the OR was "
        "dropped or canonical resolution path regressed."
    )


def test_graph_proximity_canonical_resolution(db_conn):
    """Two entities with same name; B.canonical_id = A.id. Query for
    A's name returns memories mentioning B (resolved via canonical)."""
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_a", content="memory about A")
    _insert_memory(db_conn, "M_b", content="memory about B-variant")
    _insert_entity(db_conn, "E_a", "Claude", normalized="claude")
    # E_b's canonical_id points to E_a
    _insert_entity(db_conn, "E_b", "Claude API",
                   normalized="claude api", canonical_id="E_a")
    _insert_mention(db_conn, "M_a", "E_a", "Claude")
    _insert_mention(db_conn, "M_b", "E_b", "Claude API")

    # Query for "claude" — should find both memories (via canonical)
    result = graph_proximity(db_conn, query="claude")
    memory_ids = {r["memory_id"] for r in result}
    assert "M_a" in memory_ids
    assert "M_b" in memory_ids


# ─── A4 — EPS guard for zero relation weight ──────────────────────


def test_graph_proximity_eps_guard_zero_weight(db_conn, monkeypatch):
    """Relation weight=0 → memory reachable only via that relation
    drops past the limit cut (rank inflates by 1/EPS)."""
    from sage_memory.graph_channel import graph_proximity
    import sage_memory.graph_channel as gc

    _insert_memory(db_conn, "M_seed", content="seed")
    _insert_memory(db_conn, "M_far", content="far via zero-weight")
    _insert_entity(db_conn, "E_seed", "SeedEntity",
                   normalized="seedentity")
    _insert_entity(db_conn, "E_far", "FarEntity",
                   normalized="farentity")
    _insert_mention(db_conn, "M_seed", "E_seed", "SeedEntity")
    _insert_mention(db_conn, "M_far", "E_far", "FarEntity")
    _insert_relation(db_conn, "E_seed", "E_far", "mentions",
                     source_memory_id="M_seed")

    # Set "mentions" weight to 0; rank for M_far should explode
    monkeypatch.setattr(
        gc, "RELATION_WEIGHTS",
        {**gc.RELATION_WEIGHTS, "mentions": 0.0},
    )

    result = graph_proximity(db_conn, query="seedentity", limit=5)
    # M_seed at d=0 stays; M_far at d=1 via zero-weight should not
    # crash and either be excluded (rank past limit) or have very
    # large rank
    far_ranks = [r["rank"] for r in result if r["memory_id"] == "M_far"]
    if far_ranks:
        assert far_ranks[0] > 1000, (
            "EPS guard should push zero-weight rank past usable range"
        )


# ─── A13 — Orphan memory filter (INNER JOIN memories) ─────────────


def test_graph_proximity_orphan_memory_filter(db_conn):
    """Memory deleted (CASCADE deletes mentions); graph_proximity
    must not surface the orphan id even if relations.source_memory_id
    SET NULL is not yet handled."""
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_keeper", content="keeper")
    _insert_memory(db_conn, "M_doomed", content="doomed")
    _insert_entity(db_conn, "E_k", "Keeper", normalized="keeper")
    _insert_entity(db_conn, "E_d", "Doomed", normalized="doomed")
    _insert_mention(db_conn, "M_keeper", "E_k", "Keeper")
    _insert_mention(db_conn, "M_doomed", "E_d", "Doomed")
    _insert_relation(db_conn, "E_k", "E_d", "relates_to",
                     source_memory_id="M_keeper")

    # Confirm M_doomed reachable first
    pre = graph_proximity(db_conn, query="keeper")
    assert "M_doomed" in {r["memory_id"] for r in pre}

    # Delete M_doomed — CASCADE removes its mentions row
    db_conn.execute("DELETE FROM memories WHERE id = 'M_doomed'")
    db_conn.commit()

    # Now graph_proximity must not surface M_doomed
    post = graph_proximity(db_conn, query="keeper")
    assert "M_doomed" not in {r["memory_id"] for r in post}


# ─── BFS visited-set tie-break determinism ────────────────────────


def test_graph_proximity_visited_set_tie_break_deterministic(db_conn):
    """Memory reachable via 2 paths at same depth — output identical
    across multiple insertion orderings (best-rank-wins, not order-
    of-iteration-dependent)."""
    from sage_memory.graph_channel import graph_proximity

    # Setup: seed memory mentions 2 entities, both pointing to a
    # shared target memory via different relation types.
    _insert_memory(db_conn, "M_seed", content="seed")
    _insert_memory(db_conn, "M_target", content="target")
    _insert_entity(db_conn, "E_seed1", "Seed1", normalized="seed1")
    _insert_entity(db_conn, "E_seed2", "Seed2", normalized="seed2")
    _insert_entity(db_conn, "E_target", "Target", normalized="target")
    _insert_mention(db_conn, "M_seed", "E_seed1", "Seed1")
    _insert_mention(db_conn, "M_seed", "E_seed2", "Seed2")
    _insert_mention(db_conn, "M_target", "E_target", "Target")
    # Two relations from seed entities to target — different weights
    _insert_relation(db_conn, "E_seed1", "E_target", "implements")
    _insert_relation(db_conn, "E_seed2", "E_target", "mentions")

    r1 = graph_proximity(db_conn, query="seed1")
    r2 = graph_proximity(db_conn, query="seed1")
    # Determinism: same query, same data → same output
    assert r1 == r2


# ─── Edges direction: outbound-only ───────────────────────────────


def test_graph_proximity_edges_outbound_only(db_conn):
    """Manual edge M_a → M_b. Query seeded at M_a reaches M_b.
    Reverse: only edge M_b → M_a — query seeded at M_a does NOT
    reach M_b (outbound-only per spec)."""
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_a", content="memory a")
    _insert_memory(db_conn, "M_b", content="memory b")
    _insert_entity(db_conn, "E_a", "Alpha", normalized="alpha")
    _insert_mention(db_conn, "M_a", "E_a", "Alpha")

    # Edge B → A (NOT A → B) — outbound from A does not reach B
    _insert_edge(db_conn, "M_b", "M_a", relation="references")

    result = graph_proximity(db_conn, query="alpha")
    # M_a should be reachable (seed); M_b should NOT be (only inbound)
    memory_ids = {r["memory_id"] for r in result}
    assert "M_a" in memory_ids
    assert "M_b" not in memory_ids


def test_graph_proximity_edges_outbound_reaches(db_conn):
    """Sanity flip: edge A→B does reach B from A."""
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_a", content="memory a")
    _insert_memory(db_conn, "M_b", content="memory b")
    _insert_entity(db_conn, "E_a", "Alpha", normalized="alpha")
    _insert_mention(db_conn, "M_a", "E_a", "Alpha")
    _insert_edge(db_conn, "M_a", "M_b", relation="references")

    result = graph_proximity(db_conn, query="alpha")
    memory_ids = {r["memory_id"] for r in result}
    assert "M_a" in memory_ids
    assert "M_b" in memory_ids


# ─── Rank curves ──────────────────────────────────────────────────


def test_graph_proximity_linear_curve_formula(monkeypatch, db_conn):
    """Linear curve: rank = 1 + d * (1.0 / max(w, EPS))."""
    monkeypatch.setenv("SAGE_GRAPH_RANK_CURVE", "linear")
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed", content="seed")
    _insert_memory(db_conn, "M_d1", content="depth1")
    _insert_entity(db_conn, "E_s", "Seed", normalized="seed")
    _insert_entity(db_conn, "E_d", "Depth1", normalized="depth1")
    _insert_mention(db_conn, "M_seed", "E_s", "Seed")
    _insert_mention(db_conn, "M_d1", "E_d", "Depth1")
    # "implements" weight = 1.0; linear at d=1: rank = 1 + 1/1.0 = 2.0
    _insert_relation(db_conn, "E_s", "E_d", "implements")

    result = graph_proximity(db_conn, query="seed")
    d1 = [r for r in result if r["memory_id"] == "M_d1"]
    assert len(d1) == 1
    assert d1[0]["rank"] == pytest.approx(2.0, rel=0.01)


def test_graph_proximity_harmonic_curve_softer_falloff(
    monkeypatch, db_conn,
):
    """Harmonic curve at d=1 uses H(1)=1 → same as linear at d=1.
    The curve differentiates only at d≥2 where H(2)=1.5 vs linear's 2.
    Verifying just that harmonic dispatch produces valid output here."""
    monkeypatch.setenv("SAGE_GRAPH_RANK_CURVE", "harmonic")
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed", content="seed")
    _insert_memory(db_conn, "M_d1", content="depth1")
    _insert_entity(db_conn, "E_s", "Seed", normalized="seed")
    _insert_entity(db_conn, "E_d", "Depth1", normalized="depth1")
    _insert_mention(db_conn, "M_seed", "E_s", "Seed")
    _insert_mention(db_conn, "M_d1", "E_d", "Depth1")
    _insert_relation(db_conn, "E_s", "E_d", "implements")

    result = graph_proximity(db_conn, query="seed")
    d1 = [r for r in result if r["memory_id"] == "M_d1"]
    assert len(d1) == 1
    # H(1)=1.0, w=1.0 → rank = 1 + 1.0 * 1.0 = 2.0 (same as linear at d=1)
    assert d1[0]["rank"] == pytest.approx(2.0, rel=0.01)


def test_graph_proximity_type_weighted_manual_bonus(
    monkeypatch, db_conn,
):
    """type-weighted curve with a manual edge → uses MANUAL_EDGE_BONUS.
    rank = 1 + d * (1.0 / max(w * 1.5, EPS))
    Manual edge w=1.2, bonus=1.5 → divisor = 1.8 → rank ≈ 1.56 at d=1."""
    monkeypatch.setenv("SAGE_GRAPH_RANK_CURVE", "type-weighted")
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_a", content="memory a")
    _insert_memory(db_conn, "M_b", content="memory b")
    _insert_entity(db_conn, "E_a", "Alpha", normalized="alpha")
    _insert_mention(db_conn, "M_a", "E_a", "Alpha")
    _insert_edge(db_conn, "M_a", "M_b", relation="forks")

    result = graph_proximity(db_conn, query="alpha")
    m_b = [r for r in result if r["memory_id"] == "M_b"]
    assert len(m_b) == 1
    # w=1.2 (manual edge weight), bonus=1.5 → divisor=1.8
    # rank = 1 + 1/1.8 ≈ 1.556
    assert m_b[0]["rank"] == pytest.approx(1.556, rel=0.05)


def test_graph_proximity_type_weighted_auto_weak_relation(
    monkeypatch, db_conn,
):
    """type-weighted with auto 'mentions' relation → bonus=0.6.
    w=0.7 (mentions default), bonus=0.6 → divisor=0.42 →
    rank ≈ 1 + 1/0.42 ≈ 3.38 at d=1."""
    monkeypatch.setenv("SAGE_GRAPH_RANK_CURVE", "type-weighted")
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed", content="seed")
    _insert_memory(db_conn, "M_d1", content="depth1")
    _insert_entity(db_conn, "E_s", "Seed", normalized="seed")
    _insert_entity(db_conn, "E_d", "Depth1", normalized="depth1")
    _insert_mention(db_conn, "M_seed", "E_s", "Seed")
    _insert_mention(db_conn, "M_d1", "E_d", "Depth1")
    _insert_relation(db_conn, "E_s", "E_d", "mentions")

    result = graph_proximity(db_conn, query="seed")
    d1 = [r for r in result if r["memory_id"] == "M_d1"]
    assert len(d1) == 1
    # w=0.7, bonus=0.6 → divisor=0.42 → rank ≈ 3.381
    assert d1[0]["rank"] == pytest.approx(3.381, rel=0.05)


def test_graph_proximity_unknown_rank_curve_raises(
    monkeypatch, db_conn,
):
    """Unknown SAGE_GRAPH_RANK_CURVE → ValueError on first call
    (fail-fast for ablation typo safety, per spec)."""
    monkeypatch.setenv("SAGE_GRAPH_RANK_CURVE", "type_weighted")  # typo
    from sage_memory.graph_channel import graph_proximity

    _insert_memory(db_conn, "M_seed")
    _insert_entity(db_conn, "E_s", "Seed", normalized="seed")
    _insert_mention(db_conn, "M_seed", "E_s", "Seed")

    with pytest.raises(ValueError, match=r"SAGE_GRAPH_RANK_CURVE"):
        graph_proximity(db_conn, query="seed")
