"""T1 — semantic_dedup.find_near_duplicate + _format_near_dup_entry.

Module-level tests that exercise the dedup logic directly (without
going through store()/update() — T2 covers those integration paths).

Tests use direct SQL seeding of `memories` + `memories_vec` so the
production write path is not a dependency. Acceptance scenarios
#1, #2 use FastEmbedder via the `embedder_fastembed` conftest
fixture (T0). Boundary tests (#6, #8, #9, #10) use synthetic
vectors and are embedder-independent.
"""

from __future__ import annotations

import math
import sqlite3
import time
import uuid

import pytest

from sage_memory.db import _open, get_project_db_path
from sage_memory.embedder import EMBEDDING_DIM, serialize_vec


# ─── Test fixtures ────────────────────────────────────────────────


@pytest.fixture
def project_db(tmp_path):
    """A fully-migrated project DB at tmp_path/.sage-memory/project.db.
    No worker, no extraction queue activity."""
    db = _open(get_project_db_path(tmp_path))
    yield db
    db.close()


def _insert_memory(db, *, memory_id, title, content, embedding, status="active"):
    """Seed one memory + its vec row directly via SQL."""
    now = time.time()
    db.execute(
        """INSERT INTO memories
           (id, title, content, tags, content_hash, embedded, status,
            created_at, updated_at, accessed_at, access_count)
           VALUES (?, ?, ?, '[]', ?, 1, ?, ?, ?, ?, 0)""",
        (memory_id, title, content, f"hash_{memory_id}", status, now, now, now),
    )
    db.execute(
        "INSERT INTO memories_vec (memory_id, embedding) VALUES (?, ?)",
        (memory_id, serialize_vec(embedding)),
    )
    db.commit()


def _unit_vector(seed: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic unit vector for synthetic testing.

    Constructs a vector with a few non-zero entries derived from the
    seed, then L2-normalizes. Same seed → same vector.
    """
    import random
    r = random.Random(seed)
    raw = [r.gauss(0, 1) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _vector_at_cosine(target: list[float], cosine: float) -> list[float]:
    """Construct a unit vector at a specific cosine similarity to `target`.

    Uses Gram-Schmidt: u = cosine * target + sin(theta) * orthogonal,
    where orthogonal is a unit vector perpendicular to target.
    """
    import random
    # Pick a random direction; orthogonalize against target.
    r = random.Random(hash(("orth", cosine)) & 0xFFFFFFFF)
    raw = [r.gauss(0, 1) for _ in range(len(target))]
    dot = sum(a * b for a, b in zip(raw, target))
    orth = [a - dot * b for a, b in zip(raw, target)]
    on = math.sqrt(sum(x * x for x in orth))
    if on == 0:
        raise ValueError("orthogonal collapsed; retry with different seed")
    orth = [x / on for x in orth]
    sin_theta = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return [cosine * t + sin_theta * o for t, o in zip(target, orth)]


# ─── Smoke test: distance↔similarity identity ─────────────────────


def test_distance_to_similarity_identity_holds_at_known_cosine(project_db):
    """Risk-front-load test. The whole module assumes
    `cos = 1 - (L2**2)/2` for unit vectors. Verify on a synthetic
    pair at known cosine 0.99 before any production wiring matters.
    """
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=1)
    neighbor = _vector_at_cosine(target, 0.99)
    # Renormalize because float arithmetic in _vector_at_cosine can
    # leave tiny norm drift — semantic_dedup's defensive normalize
    # handles this in production, but for the smoke test we want a
    # pure-formula verification.
    n_norm = math.sqrt(sum(x * x for x in neighbor))
    neighbor = [x / n_norm for x in neighbor]

    _insert_memory(
        project_db,
        memory_id="M_target",
        title="target",
        content="content",
        embedding=target,
    )
    result = find_near_duplicate(project_db, embedding=neighbor, threshold=0.95)
    assert result is not None
    assert result["target_id"] == "M_target"
    # Similarity returned should be within 0.005 of the known 0.99.
    assert abs(result["similarity"] - 0.99) < 0.005, (
        f"similarity {result['similarity']} drifted >0.005 from known 0.99 "
        f"(distance↔similarity identity may be wrong)"
    )


# ─── (#1) Paraphrase caught — FastEmbedder ────────────────────────


def test_paraphrase_caught_via_fastembedder(project_db, embedder_fastembed):
    """Hand-authored paraphrase pair (PRE-VERIFIED at cosine 0.9896
    under bge-small-en-v1.5; the spec's original example "the payment
    service uses saga" sat at 0.9105, which was below threshold —
    swapped per T0 risk mitigation)."""
    from sage_memory.semantic_dedup import find_near_duplicate

    a_text = "PaymentOrchestrator uses the saga pattern for distributed transactions"
    b_text = "PaymentOrchestrator implements distributed transactions via the saga pattern"

    a_emb = embedder_fastembed.embed(a_text)
    b_emb = embedder_fastembed.embed(b_text)

    _insert_memory(project_db, memory_id="M_a", title="A", content=a_text, embedding=a_emb)
    result = find_near_duplicate(project_db, embedding=b_emb)
    assert result is not None, (
        f"paraphrase pair did not clear threshold under FastEmbedder; "
        f"swap to a pre-verified pair (see T0 risk mitigation)"
    )
    assert result["target_id"] == "M_a"
    assert result["similarity"] >= 0.95


# ─── (#2) Distinct memory NOT flagged — FastEmbedder ──────────────


def test_distinct_topic_not_flagged_via_fastembedder(project_db, embedder_fastembed):
    """Same topic (PaymentOrchestrator) but different mechanism
    (saga vs Kafka) must NOT cross 0.95 cosine."""
    from sage_memory.semantic_dedup import find_near_duplicate

    a_text = "PaymentOrchestrator uses the saga pattern for distributed transactions"
    b_text = "PaymentOrchestrator emits domain events via Kafka topics"

    a_emb = embedder_fastembed.embed(a_text)
    b_emb = embedder_fastembed.embed(b_text)

    _insert_memory(project_db, memory_id="M_a", title="A", content=a_text, embedding=a_emb)
    result = find_near_duplicate(project_db, embedding=b_emb)
    # Either no match (None) or below threshold means: not flagged as near_dup.
    if result is not None:
        assert result["similarity"] < 0.95, (
            f"distinct topic falsely flagged at similarity {result['similarity']}"
        )


# ─── (#3) Self-suggestion impossible (k=5 proves it) ──────────────


def test_self_suggestion_impossible_with_exclude_id(project_db):
    """When only the self-row exists, exclude_id filtering returns None."""
    from sage_memory.semantic_dedup import find_near_duplicate

    vec = _unit_vector(seed=42)
    _insert_memory(project_db, memory_id="M_self", title="self", content="x", embedding=vec)

    result = find_near_duplicate(project_db, embedding=vec, exclude_id="M_self")
    assert result is None, "exclude_id filter failed; self-match returned"


def test_self_excluded_but_other_paraphrase_returned(project_db):
    """Regression guard against the k=1 design bug: when both the
    self-row AND a paraphrase exist, the self gets filtered and the
    paraphrase is returned. k=5 gives the headroom for this."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=10)
    paraphrase = _vector_at_cosine(target, 0.97)

    _insert_memory(project_db, memory_id="M_self", title="self", content="x", embedding=target)
    _insert_memory(project_db, memory_id="M_para", title="para", content="y", embedding=paraphrase)

    result = find_near_duplicate(project_db, embedding=target, exclude_id="M_self")
    assert result is not None
    assert result["target_id"] == "M_para"


# ─── (#5-style) No-vec-table graceful degradation ─────────────────


def test_returns_none_when_memories_vec_missing(tmp_path):
    """When `memories_vec` table doesn't exist (e.g., a stripped DB),
    sqlite3.OperationalError is caught and None returned."""
    from sage_memory.semantic_dedup import find_near_duplicate

    # Bare sqlite — no sqlite-vec loaded, no migrations.
    db = sqlite3.connect(str(tmp_path / "bare.db"))
    db.row_factory = sqlite3.Row
    vec = _unit_vector(seed=1)
    result = find_near_duplicate(db, embedding=vec)
    assert result is None
    db.close()


# ─── (#6) Borderline threshold — synthetic ────────────────────────


def test_below_threshold_returns_none(project_db):
    """A neighbor at cosine 0.93 (below default threshold 0.95) yields None."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=100)
    neighbor = _vector_at_cosine(target, 0.93)
    _insert_memory(project_db, memory_id="M_n", title="n", content="x", embedding=neighbor)

    result = find_near_duplicate(project_db, embedding=target)
    assert result is None


def test_above_threshold_returned(project_db):
    """A neighbor at cosine 0.97 (above default 0.95) is returned."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=200)
    neighbor = _vector_at_cosine(target, 0.97)
    _insert_memory(project_db, memory_id="M_n", title="n", content="x", embedding=neighbor)

    result = find_near_duplicate(project_db, embedding=target)
    assert result is not None
    assert result["target_id"] == "M_n"
    assert abs(result["similarity"] - 0.97) < 0.01


# ─── (#8) Threshold parameter plumbed ─────────────────────────────


def test_threshold_kwarg_overrides_default(project_db):
    """Custom threshold raises the bar; 0.97-sim pair excluded at threshold=0.99."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=300)
    neighbor = _vector_at_cosine(target, 0.97)
    _insert_memory(project_db, memory_id="M_n", title="n", content="x", embedding=neighbor)

    assert find_near_duplicate(project_db, embedding=target, threshold=0.99) is None
    assert find_near_duplicate(project_db, embedding=target, threshold=0.90) is not None


# ─── (#9) k parameter + invalidated headroom ──────────────────────


def test_k_param_invalidated_headroom(project_db):
    """4 invalidated rows close to target + 1 active eligible farther:
    k=5 returns the active one (invalidated filtered post-LIMIT).
    k=3 would miss because the active falls outside top-3."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=400)
    # 4 very-close invalidated rows (cosine ~0.999)
    for i in range(4):
        v = _vector_at_cosine(target, 0.999)
        _insert_memory(
            project_db, memory_id=f"M_inv_{i}", title=f"inv_{i}",
            content="x", embedding=v, status="invalidated",
        )
    # 1 farther active row (cosine ~0.96)
    active_v = _vector_at_cosine(target, 0.96)
    _insert_memory(
        project_db, memory_id="M_active", title="active",
        content="x", embedding=active_v, status="active",
    )

    # k=5 → all 4 invalidated + the active candidate fetched; filter
    # drops the 4; active wins.
    r5 = find_near_duplicate(project_db, embedding=target, k=5)
    assert r5 is not None
    assert r5["target_id"] == "M_active"

    # k=3 → only 3 of the 4 invalidated fetched; active is at rank 5
    # in distance order so not in the top-3 → None.
    r3 = find_near_duplicate(project_db, embedding=target, k=3)
    assert r3 is None


# ─── (#10) Non-normalized defensively normalized ──────────────────


def test_non_normalized_input_defensively_normalized(project_db):
    """Doubling a unit vector (norm=2) gives same dedup result as
    the unit vector — defensive normalize handles the rescale."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=500)
    neighbor = _vector_at_cosine(target, 0.97)
    _insert_memory(project_db, memory_id="M_n", title="n", content="x", embedding=neighbor)

    unit_result = find_near_duplicate(project_db, embedding=target)
    scaled_target = [x * 2.0 for x in target]
    scaled_result = find_near_duplicate(project_db, embedding=scaled_target)

    assert unit_result is not None and scaled_result is not None
    assert unit_result["target_id"] == scaled_result["target_id"]
    # Similarities should match within float tolerance.
    assert abs(unit_result["similarity"] - scaled_result["similarity"]) < 0.001


def test_zero_vector_returns_none(project_db):
    """All-zero embedding has norm 0; defensive check returns None
    instead of crashing on division-by-zero."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=600)
    _insert_memory(project_db, memory_id="M_t", title="t", content="x", embedding=target)

    result = find_near_duplicate(project_db, embedding=[0.0] * EMBEDDING_DIM)
    assert result is None


def test_nan_vector_returns_none(project_db):
    """NaN in input makes norm non-finite; defensive check returns
    None instead of propagating NaN through the cosine comparison."""
    from sage_memory.semantic_dedup import find_near_duplicate

    target = _unit_vector(seed=700)
    _insert_memory(project_db, memory_id="M_t", title="t", content="x", embedding=target)

    bad = list(target)
    bad[0] = float("nan")
    result = find_near_duplicate(project_db, embedding=bad)
    assert result is None


# ─── Helper unit tests (h1, h2) ───────────────────────────────────


def test_format_near_dup_entry_shape():
    """All 5 fields present, similarity rounded to 2 decimals,
    `reason` uses ASCII `>=` not Unicode `≥`."""
    from sage_memory.semantic_dedup import _format_near_dup_entry

    entry = _format_near_dup_entry({
        "target_id": "M_a", "target_title": "title", "similarity": 0.971234,
    })
    assert set(entry.keys()) == {
        "target_id", "target_title", "reason", "confidence", "similarity",
    }
    assert entry["target_id"] == "M_a"
    assert entry["target_title"] == "title"
    assert entry["confidence"] == "near_duplicate"
    assert entry["similarity"] == 0.97  # rounded
    assert "≥" not in entry["reason"], "reason must use ASCII >=, not Unicode ≥"
    assert ">=" in entry["reason"]


def test_format_near_dup_entry_reads_module_threshold(monkeypatch):
    """Exercises the spec's 'no kwarg' decision: helper reads module
    THRESHOLD; patching it changes the reason string output."""
    import sage_memory.semantic_dedup as sd

    monkeypatch.setattr(sd, "THRESHOLD", 0.99)
    entry = sd._format_near_dup_entry({
        "target_id": "M_a", "target_title": "t", "similarity": 0.995,
    })
    assert "0.99" in entry["reason"], (
        f"reason {entry['reason']!r} did not pick up monkeypatched "
        f"THRESHOLD=0.99 — helper may be ignoring the module constant"
    )


# ══════════════════════════════════════════════════════════════════
# T2 — _safe_suggest extension + store()/update() plumbing
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def project_db_fastembed(tmp_path, monkeypatch):
    """Project DB rooted at tmp_path with FastEmbedder active.

    Mirrors the project_db_hq pattern from test_search_3channel.py
    but using the real FastEmbedder so paraphrase-cosine assertions
    are realistic.
    """
    from sage_memory.db import (
        _open, get_project_db_path, close_all, override_project_root,
    )
    from sage_memory.embedder import (
        FastEmbedder, LocalEmbedder, set_embedder,
    )
    import sage_memory.db as _db_mod
    import sage_memory.search as _search_mod

    pytest.importorskip("fastembed")

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(FastEmbedder())
    db = _open(get_project_db_path(tmp_path))

    tmp_global = tmp_path / "global_test.db"
    tmp_global_conn = _open(tmp_global)

    def fake_get_global_db():
        return tmp_global_conn

    def fake_get_all_dbs():
        return [("project", db), ("global", tmp_global_conn)]

    monkeypatch.setattr(_db_mod, "get_global_db", fake_get_global_db)
    monkeypatch.setattr(_db_mod, "get_all_dbs", fake_get_all_dbs)
    monkeypatch.setattr(_search_mod, "get_all_dbs", fake_get_all_dbs)

    yield db
    close_all()
    set_embedder(LocalEmbedder())


# ─── (T2a) FTS-only path when embedding=None ──────────────────────


def test_safe_suggest_fts_only_when_embedding_none(project_db):
    """Regression guard: existing 0.9.0 `suggested_links` shape
    preserved when no embedding is passed (e.g., no `[neural]`
    extra, threshold-too-low, or _try_embed raised)."""
    from sage_memory.store import _safe_suggest

    # Seed a memory whose title+content has keyword overlap so FTS
    # will surface it. Embed it under LocalEmbedder so the vec table
    # has a row (though we won't use it).
    from sage_memory.embedder import LocalEmbedder
    le = LocalEmbedder()
    a_text = (
        "PostgreSQL connection pooling configuration with PgBouncer "
        "for production workloads"
    )
    _insert_memory(
        project_db, memory_id="M_pg", title="PostgreSQL pooling",
        content=a_text, embedding=le.embed(a_text),
    )

    # Call _safe_suggest with embedding=None — should fall through to
    # the existing FTS path. The result must NOT contain any entry
    # with confidence='near_duplicate'.
    result = _safe_suggest(
        project_db,
        "PostgreSQL connection pooling tips",
        exclude_id=None,
        embedding=None,
    )
    assert isinstance(result, list)
    for entry in result:
        assert entry.get("confidence") != "near_duplicate", (
            f"FTS-only path produced a near_duplicate entry: {entry}"
        )


# ─── (T2b) Merge dedup — same target from both paths ──────────────


def test_safe_suggest_dedup_merges_overlapping_target(project_db):
    """When near-duplicate AND FTS surface the SAME memory, the
    merged result has exactly one entry for that target id, and the
    entry carries the near_duplicate confidence (semantic signal
    wins; FTS entry suppressed)."""
    from sage_memory.store import _safe_suggest
    from sage_memory.embedder import LocalEmbedder
    le = LocalEmbedder()

    # Seed memory with content that LocalEmbedder will both:
    #   (a) embed similarly to a near-cosine probe, AND
    #   (b) FTS-match against shared keywords.
    a_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    a_emb = le.embed(a_text)
    _insert_memory(
        project_db, memory_id="M_a", title="alpha beta",
        content=a_text, embedding=a_emb,
    )

    # Build a probe vector that's a near-paraphrase of a_emb (so
    # semantic dedup fires). LocalEmbedder is char-ngram TF-IDF —
    # the same content gives the same vector, so re-embedding the
    # same text guarantees cosine 1.0.
    probe_emb = le.embed(a_text)
    probe_content = "alpha beta gamma delta epsilon"  # FTS will hit M_a

    result = _safe_suggest(
        project_db, probe_content,
        exclude_id=None, embedding=probe_emb,
    )
    # Exactly one entry for M_a, carrying near_duplicate confidence.
    matches = [e for e in result if e["target_id"] == "M_a"]
    assert len(matches) == 1, (
        f"merge dedup failed; expected 1 entry for M_a, got {matches}"
    )
    assert matches[0]["confidence"] == "near_duplicate"


# ─── (T2c) Spec #11 — update() content change surfaces signal ─────


def test_update_content_change_surfaces_dedup_signal(
    project_db_fastembed, embedder_fastembed,
):
    """Store A (saga pattern); store B with DISTINCT content (so the
    initial store of B does NOT trigger the signal); update B's
    content to a verified paraphrase of A. The update response must
    carry a `near_duplicate` entry pointing at A — exercises T2's
    plumbing of `fresh_embedding` through update()."""
    from sage_memory.store import store, update

    a_text = "PaymentOrchestrator uses the saga pattern for distributed transactions"
    distinct_b_text = "The CSS preprocessor uses nested rules for theme variants"
    paraphrase_text = (
        "PaymentOrchestrator implements distributed transactions via the saga pattern"
    )

    a_res = store(content=a_text, title="A")
    assert a_res["success"]
    b_res = store(content=distinct_b_text, title="B")
    assert b_res["success"]

    # Initial store of B should NOT have flagged A (distinct topic).
    initial_dups = [
        e for e in b_res.get("suggested_links", [])
        if e.get("confidence") == "near_duplicate"
    ]
    assert not initial_dups, (
        f"distinct B falsely flagged on initial store: {initial_dups}"
    )

    # Update B's content to the paraphrase. Now the dedup signal
    # should fire pointing at A.
    upd = update(id=b_res["id"], content=paraphrase_text)
    assert upd["success"]
    near_dups = [
        e for e in upd.get("suggested_links", [])
        if e.get("confidence") == "near_duplicate"
    ]
    assert len(near_dups) >= 1
    assert any(e["target_id"] == a_res["id"] for e in near_dups), (
        f"update() did not surface paraphrase of A in suggested_links: {upd}"
    )


# ─── (T2 +) Spec #4 — Cross-project isolation (write-side) ────────


def test_safe_suggest_does_not_cross_project_boundaries(tmp_path, monkeypatch):
    """Spec #4: two separate project DBs. Storing a paraphrase in
    project B must NOT surface the paraphrase from project A.
    Verifies the write-side dedup is scoped to the caller's `db`
    connection."""
    from sage_memory.db import (
        _open, close_all, get_project_db_path, override_project_root,
    )
    from sage_memory.embedder import LocalEmbedder
    from sage_memory.store import _safe_suggest

    # Two independent project roots → two independent DBs.
    proj_a_root = tmp_path / "proj_a"
    proj_b_root = tmp_path / "proj_b"
    proj_a_root.mkdir()
    proj_b_root.mkdir()

    le = LocalEmbedder()
    a_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"

    # Project A: open, seed A, close.
    close_all()
    override_project_root(proj_a_root)
    db_a = _open(get_project_db_path(proj_a_root))
    _insert_memory(
        db_a, memory_id="M_a_proj_a", title="A in project A",
        content=a_text, embedding=le.embed(a_text),
    )
    db_a.close()
    close_all()

    # Project B: open, call _safe_suggest with a paraphrase probe.
    # The connection passed to _safe_suggest is project B's DB; the
    # dedup check should ONLY see project B's vectors (which is empty).
    override_project_root(proj_b_root)
    db_b = _open(get_project_db_path(proj_b_root))
    try:
        result = _safe_suggest(
            db_b, a_text, exclude_id=None, embedding=le.embed(a_text),
        )
        near_dups = [e for e in result if e.get("confidence") == "near_duplicate"]
        assert not near_dups, (
            f"cross-project leak: project B's _safe_suggest surfaced a "
            f"near_duplicate from project A: {near_dups}"
        )
    finally:
        db_b.close()
        close_all()


# ─── (T2 +) Spec #4a — Cross-scope isolation (project vs global) ──


def test_safe_suggest_does_not_cross_scope_boundaries(tmp_path):
    """Spec #4a: seed memory A in global scope; call _safe_suggest
    with a paraphrase probe against a project-scope DB. The
    project-scope dedup check must NOT surface A from global.
    Search at read time queries both DBs, but dedup is write-side
    and follows the caller's `db` connection."""
    from sage_memory.db import _open, get_project_db_path
    from sage_memory.embedder import LocalEmbedder
    from sage_memory.store import _safe_suggest

    le = LocalEmbedder()
    a_text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"

    # Two physical DB files: "global" and "project".
    global_db = _open(tmp_path / "global.db")
    project_db_conn = _open(get_project_db_path(tmp_path / "proj"))
    try:
        _insert_memory(
            global_db, memory_id="M_a_global", title="A in global",
            content=a_text, embedding=le.embed(a_text),
        )
        # _safe_suggest called with the project DB connection.
        result = _safe_suggest(
            project_db_conn, a_text, exclude_id=None,
            embedding=le.embed(a_text),
        )
        near_dups = [e for e in result if e.get("confidence") == "near_duplicate"]
        assert not near_dups, (
            f"cross-scope leak: project _safe_suggest surfaced a "
            f"near_duplicate from global: {near_dups}"
        )
    finally:
        global_db.close()
        project_db_conn.close()


# ─── (T2 +) find_near_duplicate raising → FTS-only fallback ───────


def test_safe_suggest_falls_back_when_find_near_duplicate_raises(
    project_db, monkeypatch,
):
    """Defensive branch: when `find_near_duplicate` raises a
    non-sqlite exception (e.g., a bug in our code or an embedder
    glitch), `_safe_suggest` logs at WARNING and falls through to
    FTS-only suggestions instead of crashing the store/update path."""
    from sage_memory.store import _safe_suggest
    from sage_memory.embedder import LocalEmbedder
    import sage_memory.store as _store_mod

    le = LocalEmbedder()
    a_text = (
        "PostgreSQL connection pooling configuration with PgBouncer"
    )
    _insert_memory(
        project_db, memory_id="M_pg", title="pg pool",
        content=a_text, embedding=le.embed(a_text),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated semantic_dedup bug")

    monkeypatch.setattr(
        _store_mod._semantic_dedup, "find_near_duplicate", _boom,
    )

    # Call shouldn't raise; result should be FTS-only (no near_dup).
    result = _safe_suggest(
        project_db, "PostgreSQL connection pooling tips",
        exclude_id=None, embedding=le.embed(a_text),
    )
    assert isinstance(result, list)
    near_dups = [e for e in result if e.get("confidence") == "near_duplicate"]
    assert not near_dups, (
        f"fallback failed: near_duplicate surfaced despite find_near_duplicate "
        f"raising: {near_dups}"
    )
    # Tighten: FTS path must have produced its own entry for M_pg
    # (the seeded memory has shared keywords with the probe). A
    # silent both-branches-drop bug would pass the no-near_dup
    # assertion above; this guards against that.
    assert any(e.get("target_id") == "M_pg" for e in result), (
        f"FTS fallback did not surface M_pg; both branches may have "
        f"silently dropped: {result}"
    )


# ─── (T2d) Spec #12 — update() status-only does NOT surface ───────


def test_update_status_only_does_not_surface_dedup_signal(
    project_db_fastembed, embedder_fastembed,
):
    """Store A; store B (paraphrase, so initial store DOES surface
    near_dup); update B's `status` to 'archived' only. The update
    response should be FTS-only (no `near_duplicate` entry) because
    `needs_reembed=False` → `fresh_embedding=None` → semantic dedup
    skipped (avoids self-match against B's stale embedding)."""
    from sage_memory.store import store, update

    a_text = "PaymentOrchestrator uses the saga pattern for distributed transactions"
    paraphrase_text = (
        "PaymentOrchestrator implements distributed transactions via the saga pattern"
    )

    a_res = store(content=a_text, title="A")
    assert a_res["success"]
    b_res = store(content=paraphrase_text, title="B")
    assert b_res["success"]

    # Sanity: initial B store DID surface the signal.
    initial_dups = [
        e for e in b_res.get("suggested_links", [])
        if e.get("confidence") == "near_duplicate"
    ]
    assert any(e["target_id"] == a_res["id"] for e in initial_dups), (
        "test setup invalid: initial store of paraphrase B should have "
        "surfaced a near_duplicate for A"
    )

    # Now update only status (no content/title change).
    upd = update(id=b_res["id"], status="archived")
    assert upd["success"]
    near_dups = [
        e for e in upd.get("suggested_links", [])
        if e.get("confidence") == "near_duplicate"
    ]
    assert not near_dups, (
        f"status-only update incorrectly surfaced near_duplicate: {near_dups}"
    )
