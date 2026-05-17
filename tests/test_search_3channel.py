"""T2 — search.py 3-channel RRF refactor.

Covers A5 (3-channel order with populated graph), A6 smoke (2-channel
degradation when graph empty), A12 (no sync writes from search),
plus vec_weight floor ADR-004 alignment.
"""

from __future__ import annotations

import time
import uuid

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.search import search
from sage_memory.store import store
import sage_memory.db as _db_mod
import sage_memory.search as _search_mod


class _HighQualityTestEmbedder:
    name = "test-hq"; version = "v1"; dim = EMBEDDING_DIM
    quality = 0.9; max_input_chars = 8192
    def embed(self, text):
        h = abs(hash(text)) % 1000
        return [(h % 7) / 7.0] * EMBEDDING_DIM


@pytest.fixture
def project_db_hq(tmp_path, monkeypatch):
    """Fresh project DB, HQ embedder, isolated global DB."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
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


@pytest.fixture
def project_db_local(tmp_path, monkeypatch):
    """Fresh project DB with LocalEmbedder (quality=0.45) for vec_weight
    floor test."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(LocalEmbedder())  # quality=0.45
    db = _open(get_project_db_path(tmp_path))

    tmp_global = tmp_path / "global_test.db"
    tmp_global_conn = _open(tmp_global)

    def fake_get_all_dbs():
        return [("project", db), ("global", tmp_global_conn)]

    monkeypatch.setattr(_db_mod, "get_all_dbs", fake_get_all_dbs)
    monkeypatch.setattr(_search_mod, "get_all_dbs", fake_get_all_dbs)

    yield db
    close_all()


def _seed_entity_graph(db):
    """Helper: insert 2 memories + 2 entities + 1 relation. The graph
    channel will surface M_neighbor when query mentions 'Alpha'."""
    now = time.time()
    db.executemany(
        "INSERT INTO memories(id, title, content, content_hash, "
        "embedded, created_at, updated_at, accessed_at) "
        "VALUES (?, ?, ?, ?, 0, 1, 1, 1)",
        [
            ("M_a", "memA", "content of memory about Alpha", "h_a"),
            ("M_b", "memB", "neighbor memory mentions Beta", "h_b"),
        ],
    )
    db.executemany(
        "INSERT INTO entities (id, name, name_normalized, type, "
        "mention_count, created_at, updated_at) "
        "VALUES (?, ?, ?, 'CONCEPT', 1, ?, ?)",
        [
            ("E_a", "Alpha", "alpha", now, now),
            ("E_b", "Beta", "beta", now, now),
        ],
    )
    db.executemany(
        "INSERT INTO mentions (memory_id, entity_id, surface_form, "
        "confidence, created_at) VALUES (?, ?, ?, 1.0, ?)",
        [
            ("M_a", "E_a", "Alpha", now),
            ("M_b", "E_b", "Beta", now),
        ],
    )
    db.execute(
        "INSERT INTO relations (id, source_entity_id, target_entity_id, "
        "relation_type, confidence, created_at) "
        "VALUES (?, 'E_a', 'E_b', 'relates_to', 1.0, ?)",
        (uuid.uuid4().hex, now),
    )
    db.commit()


# ─── A5 — 3-channel RRF with populated graph ─────────────────────


def test_search_3channel_finds_graph_neighbor(project_db_hq):
    """Query for 'Alpha' (entity name not in M_b's content). FTS alone
    would miss M_b. Graph channel surfaces M_b via the Alpha→Beta relation."""
    _seed_entity_graph(project_db_hq)

    out = search(query="Alpha", scope="project", limit=10)
    titles = [r["title"] for r in out["results"]]
    # M_a contains "Alpha" → FTS hit
    assert "memA" in titles
    # M_b only reachable via graph (its content has "Beta", not "Alpha")
    assert "memB" in titles, (
        "Graph channel should surface M_b reachable via Alpha→Beta relation"
    )


# ─── A6 smoke — 2-channel degradation when graph empty ───────────


def test_search_2channel_degradation_when_graph_empty(project_db_hq):
    """With entities empty, search() output equivalent to a 2-channel
    path on the same fixture corpus. The graph channel returns []
    immediately (fast-path) and contributes 0 to the RRF."""
    # Store some content via the regular API (entities stay empty
    # because no LLM key)
    store(content="apple banana cherry test fixture content")
    store(content="completely unrelated content about Python")

    out = search(query="apple", scope="project")
    # Sanity: at least the apple memory found
    titles = [r["title"] for r in out["results"]]
    assert len(titles) >= 1

    # Verify entities table is empty (free-path floor)
    n_entities = project_db_hq.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    assert n_entities == 0


# ─── A12 — search remains read-only with graph channel wired ────


def test_search_no_sync_writes_with_graph_channel(project_db_hq):
    """search() with populated graph triggers NO inserts/updates."""
    _seed_entity_graph(project_db_hq)

    # Snapshot row counts in tables that could be touched
    before = {
        t: project_db_hq.execute(
            f"SELECT COUNT(*) FROM {t}"
        ).fetchone()[0]
        for t in ["memories", "entities", "mentions", "relations",
                  "memories_vec", "chunks_vec",
                  "memory_embedding_meta", "chunk_embedding_meta"]
    }

    search(query="Alpha", scope="project", limit=5)

    after = {
        t: project_db_hq.execute(
            f"SELECT COUNT(*) FROM {t}"
        ).fetchone()[0]
        for t in before
    }
    assert after == before, (
        f"search() mutated tables: {after} vs baseline {before}"
    )


# ─── vec_weight floor (ADR-004 alignment) ────────────────────────


def test_search_vec_weight_floor_with_local_embedder(project_db_local):
    """LocalEmbedder quality=0.45 — but per ADR-004 vec_weight must
    use max(0.5, quality) = 0.5. Verify by inspecting the constant
    or behavior. We can't easily inspect the RRF math from outside;
    instead, verify the search.py module references the floor."""
    import sage_memory.search as search_mod
    import inspect
    src = inspect.getsource(search_mod)
    # Spec mandates the floor; should appear as max(0.5, ...) or a
    # named constant. Check both common forms.
    assert ("max(0.5" in src or "_VEC_WEIGHT_FLOOR" in src or
            "vec_weight_floor" in src.lower()), (
        "search.py must apply max(0.5, embedder.quality) floor per "
        "ADR-004; bare embedder.quality is an ADR-004 deviation"
    )


def test_search_local_embedder_below_vec_threshold(project_db_local):
    """Sanity: LocalEmbedder quality (0.45) is below the search vec
    threshold (0.6) — so use_vec is False and vec channels don't run
    regardless of the floor. The floor matters only if quality is
    between 0.5 and 0.6, but the threshold check happens first."""
    embedder_quality = LocalEmbedder().quality
    assert embedder_quality < 0.6  # below the vec threshold
    # Sanity that no crash occurs
    store(content="some local-embedder content")
    out = search(query="local", scope="project")
    assert out is not None
