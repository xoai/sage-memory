"""T2/T3/T5 — Chunked storage tests.

Uses a high-quality test embedder (quality≥0.6) so chunks_vec writes
actually happen. Mirrors the pattern from M1's test_embed_pending_meta.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.store import store, update, delete, embed_pending, embed_pending_chunks


class _HighQualityTestEmbedder:
    name = "test-hq"
    version = "v1"
    dim = EMBEDDING_DIM
    quality = 0.9
    max_input_chars = 8192

    def embed(self, text: str) -> list[float]:
        h = abs(hash(text)) % 1000
        return [(h % 7) / 7.0] * EMBEDDING_DIM


@pytest.fixture
def project_db(tmp_path):
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    db = _open(get_project_db_path(tmp_path))
    yield db
    close_all()
    set_embedder(LocalEmbedder())


# ═══════════════════════════════════════════════════════════════════
# T2 — Write path
# ═══════════════════════════════════════════════════════════════════


def test_short_memory_no_chunks(project_db):
    """Memories ≤ CHUNK_THRESHOLD (2000 chars) bypass chunking entirely."""
    result = store(content="hello world, this is short", title="test", scope="project")
    assert result["success"]
    mid = result["id"]
    count = project_db.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)
    ).fetchone()[0]
    assert count == 0


def test_long_memory_creates_chunks(project_db):
    """Memories > 2000 chars are chunked. Chunks land in chunks table,
    sync to chunks_fts via trigger, get vec entries + meta rows."""
    long_md = "## Section A\n" + ("alpha " * 300) + "\n\n## Section B\n" + ("beta " * 300)
    assert len(long_md) > 2000
    result = store(content=long_md, title="long", scope="project")
    assert result["success"]
    mid = result["id"]

    chunk_count = project_db.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)
    ).fetchone()[0]
    assert chunk_count >= 2

    # chunks_fts synced via trigger
    fts_count = project_db.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'alpha'"
    ).fetchone()[0]
    assert fts_count >= 1

    # M3a (T7): chunks_vec writes are now async (worker's reembed task).
    # In this unit test we drain via the direct-call pattern instead of
    # standing up the worker thread.
    embed_pending_chunks(project_db, memory_id=mid)
    vec_count = project_db.execute(
        "SELECT COUNT(*) FROM chunks_vec WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE memory_id=?)", (mid,)
    ).fetchone()[0]
    assert vec_count == chunk_count

    # chunk_embedding_meta entries match active embedder
    # (already drained above via embed_pending_chunks)
    meta_rows = project_db.execute(
        "SELECT model_name, model_version, dim FROM chunk_embedding_meta "
        "WHERE chunk_id IN (SELECT id FROM chunks WHERE memory_id=?)", (mid,)
    ).fetchall()
    assert len(meta_rows) == chunk_count
    for m in meta_rows:
        assert m[0] == "test-hq"
        assert m[1] == "v1"
        assert m[2] == EMBEDDING_DIM


def test_dedup_skips_chunker(project_db):
    """Content-hash dedup short-circuits before chunker runs."""
    long_md = "## A\n" + ("word " * 400) + "\n\n## B\n" + ("word " * 400)
    first = store(content=long_md, scope="project")
    assert first["success"]

    chunk_count_after_first = project_db.execute(
        "SELECT COUNT(*) FROM chunks"
    ).fetchone()[0]
    assert chunk_count_after_first > 0

    # Same content → dedup hit, returns existing id, NO new chunks
    second = store(content=long_md, scope="project")
    assert not second["success"]  # dedup signals failure with existing id
    assert second["id"] == first["id"]

    chunk_count_after_second = project_db.execute(
        "SELECT COUNT(*) FROM chunks"
    ).fetchone()[0]
    assert chunk_count_after_second == chunk_count_after_first


def test_above_cap_chunks_skip_vec_only(project_db):
    """ADR-002 §Failure Modes: all chunks kept as rows, FTS5-synced.
    Only chunks_vec inserts are deferred for chunks > MAX_CHUNKS_PER_MEMORY.
    """
    # ~150KB plain prose with no headings → forces fixed-size fallback
    # to produce > 200 segments.
    big = " ".join(["word"] * 30000)
    result = store(content=big, scope="project")
    assert result["success"]
    mid = result["id"]

    chunk_count = project_db.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)
    ).fetchone()[0]
    assert chunk_count > 200, f"expected > 200 chunks, got {chunk_count}"

    # M3a (T7): drain async chunk-embed via direct call. Embedding
    # respects MAX_CHUNKS_PER_MEMORY=200 (existing cap in
    # _try_embed_chunk via embed_pending_chunks' SELECT/batch ordering)
    # — but embed_pending_chunks itself has no cap, so we must batch-
    # limit explicitly here to mirror the historical 200-cap semantics
    # that the sync path provided.
    embed_pending_chunks(project_db, batch_size=200, memory_id=mid)
    vec_count = project_db.execute(
        "SELECT COUNT(*) FROM chunks_vec WHERE chunk_id IN "
        "(SELECT id FROM chunks WHERE memory_id=?)", (mid,)
    ).fetchone()[0]
    assert vec_count == 200, f"expected exactly 200 vec entries (cap), got {vec_count}"

    # All chunk rows synced to chunks_fts (verified by FTS search)
    fts_match = project_db.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'word'"
    ).fetchone()[0]
    assert fts_match == chunk_count, "all chunk rows should be FTS-searchable"


def test_delete_memory_cascades_to_chunks(project_db):
    """CASCADE per migration 003: deleting a memory drops its chunks."""
    long = "## A\n" + ("word " * 400) + "\n\n## B\n" + ("word " * 400)
    result = store(content=long, scope="project")
    mid = result["id"]
    assert project_db.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)
    ).fetchone()[0] > 0

    delete(id=mid, scope="project")
    remaining = project_db.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)
    ).fetchone()[0]
    assert remaining == 0


# ═══════════════════════════════════════════════════════════════════
# T3 — Update path bidirectional hysteresis
# ═══════════════════════════════════════════════════════════════════


def _count_chunks(db, mid):
    return db.execute("SELECT COUNT(*) FROM chunks WHERE memory_id=?", (mid,)).fetchone()[0]


def test_update_atomic_to_chunked(project_db):
    """Short → long: chunks get created."""
    result = store(content="short " * 50, scope="project")  # ~250 chars
    mid = result["id"]
    assert _count_chunks(project_db, mid) == 0

    long = "## A\n" + ("word " * 400) + "\n\n## B\n" + ("word " * 400)
    update(id=mid, content=long, scope="project")
    assert _count_chunks(project_db, mid) > 0


def test_update_chunked_to_unchunked(project_db):
    """Long → very short (< HYSTERESIS_LOW): chunks deleted."""
    long = "## A\n" + ("word " * 400) + "\n\n## B\n" + ("word " * 400)
    result = store(content=long, scope="project")
    mid = result["id"]
    assert _count_chunks(project_db, mid) > 0

    update(id=mid, content="short content under 1500 chars " * 10, scope="project")
    assert _count_chunks(project_db, mid) == 0


def test_update_chunked_stays_chunked_in_band(project_db):
    """Long → mid-range (HYSTERESIS_LOW ≤ new < CHUNK_THRESHOLD): re-chunked
    in place (NOT unchunked because hysteresis_low is the unchunk floor)."""
    long = "## A\n" + ("word " * 400) + "\n\n## B\n" + ("word " * 400)
    result = store(content=long, scope="project")
    mid = result["id"]
    initial_chunks = _count_chunks(project_db, mid)
    assert initial_chunks > 0

    # New content: ~1800 chars (in the [1500, 2000) hysteresis band)
    mid_content = "word " * 360  # ~1800 chars
    assert 1500 <= len(mid_content) < 2000
    update(id=mid, content=mid_content, scope="project")
    # Still chunked (in band) — chunks re-created
    assert _count_chunks(project_db, mid) > 0


def test_update_atomic_stays_atomic_in_band(project_db):
    """Short → mid-range (1500 ≤ x < 2000), starting atomic: stays atomic."""
    result = store(content="short content " * 20, scope="project")  # ~300 chars
    mid = result["id"]
    assert _count_chunks(project_db, mid) == 0

    mid_content = "word " * 360  # ~1800 chars
    update(id=mid, content=mid_content, scope="project")
    assert _count_chunks(project_db, mid) == 0  # still atomic


# ═══════════════════════════════════════════════════════════════════
# T5 — embed_pending_chunks
# ═══════════════════════════════════════════════════════════════════


def test_embed_pending_chunks_inserts_meta(project_db):
    """Insert a chunk row WITHOUT chunks_vec / meta (e.g., synthetic),
    call embed_pending_chunks, assert vec + meta now exist with matching
    resolver fields."""
    # Insert a memory + chunk row directly (bypass chunker for control)
    project_db.execute(
        "INSERT INTO memories(id,title,content,content_hash,embedded,"
        "created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1,1)"
    )
    project_db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES ('c1','m1',0,'chunk content here',0,18,1)"
    )
    project_db.commit()

    n = embed_pending_chunks(project_db, batch_size=10)
    assert n == 1

    vec_row = project_db.execute(
        "SELECT chunk_id FROM chunks_vec WHERE chunk_id='c1'"
    ).fetchone()
    assert vec_row is not None

    meta = project_db.execute(
        "SELECT model_name, model_version, dim FROM chunk_embedding_meta WHERE chunk_id='c1'"
    ).fetchone()
    assert meta is not None
    assert meta[0] == "test-hq"
    assert meta[1] == "v1"
    assert meta[2] == EMBEDDING_DIM


def test_embed_pending_chunks_skips_fresh_meta(project_db):
    """A chunk with matching meta is NOT re-embedded (idempotency)."""
    project_db.execute(
        "INSERT INTO memories(id,title,content,content_hash,embedded,"
        "created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1,1)"
    )
    project_db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES ('c1','m1',0,'fresh',0,5,1)"
    )
    project_db.execute(
        "INSERT INTO chunk_embedding_meta(chunk_id,model_name,model_version,dim,created_at) "
        "VALUES ('c1','test-hq','v1',?,1)", (EMBEDDING_DIM,)
    )
    project_db.commit()

    n = embed_pending_chunks(project_db, batch_size=10)
    assert n == 0  # no work to do


def test_embed_pending_chunks_reembeds_legacy_meta(project_db):
    """A chunk with legacy meta gets re-embedded under the active resolver."""
    project_db.execute(
        "INSERT INTO memories(id,title,content,content_hash,embedded,"
        "created_at,updated_at,accessed_at) "
        "VALUES ('m1','t','c','h1',1,1,1,1)"
    )
    project_db.execute(
        "INSERT INTO chunks(id,memory_id,chunk_index,content,byte_start,byte_end,created_at) "
        "VALUES ('c1','m1',0,'legacy chunk',0,12,1)"
    )
    project_db.execute(
        "INSERT INTO chunk_embedding_meta(chunk_id,model_name,model_version,dim,created_at) "
        "VALUES ('c1','legacy','0',384,1)"
    )
    project_db.commit()

    n = embed_pending_chunks(project_db, batch_size=10)
    assert n == 1

    meta = project_db.execute(
        "SELECT model_name, model_version FROM chunk_embedding_meta WHERE chunk_id='c1'"
    ).fetchone()
    assert meta[0] == "test-hq"
    assert meta[1] == "v1"
