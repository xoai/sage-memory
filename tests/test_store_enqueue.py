"""T5 — store.py enqueue semantics.

Tests A6 (extract enqueue) + A7 (per-memory reembed enqueue) without
running the worker — pure enqueue-semantics tests. Worker drain is
covered in test_worker.py.

Per rev 2 pin #1: reembed is per-memory (exactly ONE row per memory
write that produced chunks, NOT one-per-chunk).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.store import store, update


class _HighQualityTestEmbedder:
    name = "test-hq"; version = "v1"; dim = EMBEDDING_DIM
    quality = 0.9; max_input_chars = 8192
    def embed(self, text):
        return [0.1] * EMBEDDING_DIM


@pytest.fixture
def project_db_hq(tmp_path):
    """Project DB with high-quality embedder (quality > threshold)."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    db = _open(get_project_db_path(tmp_path))
    yield db
    close_all()
    set_embedder(LocalEmbedder())


@pytest.fixture
def project_db_low(tmp_path):
    """Project DB with low-quality embedder (LocalEmbedder, q=0.45)."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(LocalEmbedder())
    db = _open(get_project_db_path(tmp_path))
    yield db
    close_all()


# ─── A6 — extract enqueue ─────────────────────────────────────────


def test_store_enqueues_extract_with_llm_key(
    project_db_hq, monkeypatch,
):
    """ANTHROPIC_API_KEY set + content > 50 chars → 1 extract row."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    result = store(content="x" * 100)
    assert result["success"]
    mid = result["id"]

    rows = project_db_hq.execute(
        "SELECT task_type, status FROM extraction_queue "
        "WHERE memory_id = ? AND task_type = 'extract'", (mid,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_store_no_enqueue_without_llm_key(
    project_db_low, monkeypatch,
):
    """No LLM key + LocalEmbedder → ZERO queue rows (free-path floor)."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    result = store(content="x" * 100)
    assert result["success"]

    n = project_db_low.execute(
        "SELECT COUNT(*) FROM extraction_queue WHERE memory_id = ?",
        (result["id"],),
    ).fetchone()[0]
    assert n == 0


def test_store_no_extract_enqueue_for_short_content(
    project_db_hq, monkeypatch,
):
    """len(content) ≤ 50 → no extract enqueued even with key set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # Content needs at least 10 chars (store's minimum) but ≤ 50
    result = store(content="short content here.")  # < 50
    assert result["success"]

    n = project_db_hq.execute(
        "SELECT COUNT(*) FROM extraction_queue "
        "WHERE memory_id = ? AND task_type = 'extract'",
        (result["id"],),
    ).fetchone()[0]
    assert n == 0


def test_store_update_re_enqueues_extract(
    project_db_hq, monkeypatch,
):
    """update() with new content + LLM key → new extract row."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    result = store(content="x" * 100)
    mid = result["id"]
    # Clear initial queue rows
    project_db_hq.execute("DELETE FROM extraction_queue")
    project_db_hq.commit()

    update(id=mid, content="y" * 100)
    n = project_db_hq.execute(
        "SELECT COUNT(*) FROM extraction_queue "
        "WHERE memory_id = ? AND task_type = 'extract'", (mid,),
    ).fetchone()[0]
    assert n == 1


# ─── A7 — per-memory reembed enqueue ──────────────────────────────


def test_store_enqueues_one_reembed_per_memory_with_chunks(
    project_db_hq, monkeypatch,
):
    """Long content + HighQualityTestEmbedder → exactly 1 reembed row
    (per-memory, NOT one-per-chunk). chunks_vec stays empty after
    store() returns (sync embed removed)."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    # 5000 chars triggers chunking (CHUNK_THRESHOLD default ~2000)
    result = store(content="x " * 2500)
    mid = result["id"]

    # Exactly one reembed row enqueued
    rows = project_db_hq.execute(
        "SELECT id FROM extraction_queue "
        "WHERE memory_id = ? AND task_type = 'reembed'", (mid,),
    ).fetchall()
    assert len(rows) == 1, (
        f"expected 1 reembed row, got {len(rows)} "
        "(per rev 2 pin #1: per-memory scoping)"
    )

    # chunks_vec is EMPTY immediately after store() returns
    vec_count = project_db_hq.execute(
        "SELECT COUNT(*) FROM chunks_vec"
    ).fetchone()[0]
    assert vec_count == 0


def test_store_no_reembed_enqueue_below_quality_threshold(
    project_db_low,
):
    """LocalEmbedder (quality=0.45) → no reembed enqueued (matches the
    existing gate inside embed_pending_chunks)."""
    result = store(content="x " * 2500)
    mid = result["id"]
    n = project_db_low.execute(
        "SELECT COUNT(*) FROM extraction_queue "
        "WHERE memory_id = ? AND task_type = 'reembed'", (mid,),
    ).fetchone()[0]
    assert n == 0


def test_store_sync_chunks_vec_no_longer_written(
    project_db_hq, monkeypatch,
):
    """Long content + HQ embedder → chunks_vec is EMPTY synchronously
    after store() returns. Chunk rows still exist; only the vec
    embedding is deferred to the worker."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    result = store(content="x " * 2500)
    mid = result["id"]

    chunk_count = project_db_hq.execute(
        "SELECT COUNT(*) FROM chunks WHERE memory_id = ?", (mid,),
    ).fetchone()[0]
    vec_count = project_db_hq.execute(
        "SELECT COUNT(*) FROM chunks_vec"
    ).fetchone()[0]
    assert chunk_count > 1   # chunks were written
    assert vec_count == 0    # but no vec rows yet
