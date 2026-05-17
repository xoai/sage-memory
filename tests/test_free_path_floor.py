"""A10 — Free-path floor empty-tables assertion.

With all four LLM/embedder API keys scrubbed:
- Worker may still start (because reembed work exists)
- BUT extraction NEVER runs
- entities/mentions/relations tables stay EMPTY
- extraction_queue contains NO `extract` task type (only reembed)

This test resolves the MAJOR finding from the rev 2 auto-review.
"""

from __future__ import annotations

import time

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.store import store
from sage_memory.worker import Worker


class _HighQualityTestEmbedder:
    name = "test-hq"; version = "v1"; dim = EMBEDDING_DIM
    quality = 0.9; max_input_chars = 8192
    def embed(self, text):
        return [0.1] * EMBEDDING_DIM


@pytest.fixture
def db_file(tmp_path):
    """File-backed DB so the worker can open its own connection."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    path = get_project_db_path(tmp_path)
    _open(path)
    yield path
    close_all()
    set_embedder(LocalEmbedder())


@pytest.fixture
def db_conn(db_file):
    """Connection bridging path→connection for test assertions."""
    return _open(db_file)


def test_free_path_empty_tables(db_file, db_conn, monkeypatch):
    """A10: full free-path scrub → entities/mentions/relations all
    empty after store + worker drain; no extract queue rows."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "VOYAGE_API_KEY", "COHERE_API_KEY"]:
        monkeypatch.delenv(var, raising=False)

    # Store short + long content (long triggers chunk path)
    store(content="short test memory long enough")
    store(content="x " * 2500)  # triggers chunking

    # Worker may have started for reembed work (chunks need vec).
    worker = Worker(str(db_file), poll_interval_ms=50)
    worker.start()
    worker._wait_for_queue_empty(timeout_s=3.0)
    worker.stop()

    # Extract-side invariants (the load-bearing free-path assertions)
    n_entities = db_conn.execute(
        "SELECT COUNT(*) FROM entities"
    ).fetchone()[0]
    n_mentions = db_conn.execute(
        "SELECT COUNT(*) FROM mentions"
    ).fetchone()[0]
    n_relations = db_conn.execute(
        "SELECT COUNT(*) FROM relations"
    ).fetchone()[0]
    n_extract_queue = db_conn.execute(
        "SELECT COUNT(*) FROM extraction_queue "
        "WHERE task_type = 'extract'"
    ).fetchone()[0]

    assert n_entities == 0
    assert n_mentions == 0
    assert n_relations == 0
    assert n_extract_queue == 0
