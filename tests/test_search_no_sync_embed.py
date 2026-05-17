"""T6b — search.py no longer triggers sync embedding.

Spec A8: search is strictly read-only after M3a. memories_vec /
chunks_vec writes happen only via the worker's reembed task.
"""

from __future__ import annotations

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.search import search
from sage_memory.store import store


class _HighQualityTestEmbedder:
    name = "test-hq"; version = "v1"; dim = EMBEDDING_DIM
    quality = 0.9; max_input_chars = 8192
    def embed(self, text):
        return [0.1] * EMBEDDING_DIM


@pytest.fixture
def project_db_hq(tmp_path, monkeypatch):
    """Project DB with HQ embedder and LLM keys scrubbed."""
    for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    db = _open(get_project_db_path(tmp_path))
    yield db
    close_all()
    set_embedder(LocalEmbedder())


def test_search_does_not_embed_anything(project_db_hq):
    """Store + search → memories_vec stays empty (no sync embed in
    search path). Memory-level vec MAY have been written sync by
    _try_embed in store(), but search itself adds nothing."""
    result = store(content="x " * 2500)  # triggers chunks
    mid = result["id"]

    # Baseline: chunks_vec is empty after store (T5 verified this).
    baseline = project_db_hq.execute(
        "SELECT COUNT(*) FROM chunks_vec"
    ).fetchone()[0]
    assert baseline == 0

    # Run search — should NOT write any chunks_vec rows.
    search(query="content needle")

    after = project_db_hq.execute(
        "SELECT COUNT(*) FROM chunks_vec"
    ).fetchone()[0]
    assert after == 0


def test_search_module_does_not_import_embed_pending():
    """Regression guard against accidental re-import."""
    import sage_memory.search as search_mod
    assert "embed_pending" not in dir(search_mod)
    assert "embed_pending_chunks" not in dir(search_mod)
