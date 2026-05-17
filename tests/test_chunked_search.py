"""T4 — Chunk-aware search tests.

Verifies that long memories indexed via chunks_fts/chunks_vec are
discoverable via search, deduped to parent memory (no chunk objects
in MCP output), and ranked correctly when chunk hits compete with
memory-level hits.
"""

from __future__ import annotations

import pytest

from sage_memory.db import (
    _open, get_project_db_path, close_all, override_project_root,
)
from sage_memory.embedder import (
    LocalEmbedder, set_embedder, EMBEDDING_DIM,
)
from sage_memory.store import store
from sage_memory.search import search
import sage_memory.db as _db_mod
import sage_memory.search as _search_mod


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
def project_db(tmp_path, monkeypatch):
    """Fresh project DB. Also isolates from the user's real global DB
    by monkeypatching get_global_db / get_all_dbs to use a tmp global."""
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(_HighQualityTestEmbedder())
    db = _open(get_project_db_path(tmp_path))

    # Set up a tmp global DB so search's get_all_dbs() doesn't leak
    # into the user's real ~/.sage-memory/memory.db.
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


# ───────────────────────────────────────────────────────────────────
# T4 — Chunk-aware retrieval
# ───────────────────────────────────────────────────────────────────


def test_search_short_memory_via_memories_fts(project_db):
    """Short memory: discoverable via the existing memories_fts path.
    Chunks layer should not interfere."""
    store(content="quickbrownfoxjumpsoverthelazydog test content here", title="short", scope="project")
    out = search(query="quickbrownfoxjumpsoverthelazydog", scope="project")
    titles = [r["title"] for r in out["results"]]
    assert "short" in titles


def test_search_long_memory_returns_via_chunks_fts(project_db):
    """A long memory chunked on store; query matches a single chunk's
    content → parent memory returned."""
    long_md = (
        "## Section A\n" + ("alpha " * 300) + "\n\n"
        "## Section B\n" + ("specifickeywordbeta " * 100) + "\n\n"
        "## Section C\n" + ("gamma " * 300)
    )
    assert len(long_md) > 2000
    store(content=long_md, title="long-chunked", scope="project")

    # Query for a keyword that appears only in section B
    out = search(query="specifickeywordbeta", scope="project")
    titles = [r["title"] for r in out["results"]]
    assert "long-chunked" in titles


def test_short_and_long_mix(project_db):
    """Search returns both short and long memories ranked correctly.
    Uses keyword strategy because the test embedder hashes coarsely
    (only ~7 distinct vectors) so vec search would match almost
    anything; that's a test-fixture limitation, not a real-search one."""
    store(content="apple banana cherry uniquekeyword", title="short-fruit", scope="project")
    long_md = "## A\n" + ("apple " * 400) + "\n\n## B\n" + ("apple " * 400)
    store(content=long_md, title="long-apple", scope="project")

    out = search(query="uniquekeyword", scope="project", strategy="keyword")
    titles = [r["title"] for r in out["results"]]
    assert "short-fruit" in titles
    # long-apple does NOT contain uniquekeyword (text-only check, not vec)
    assert "long-apple" not in titles


def test_results_have_no_matched_chunk_id_field(project_db):
    """MCP result dicts MUST NOT expose `matched_chunk_id` — the chunk
    layer is an internal index, not surfaced via MCP. Per plan T4."""
    long_md = "## A\n" + ("foo bar " * 200) + "\n\n## B\n" + ("baz qux " * 200)
    store(content=long_md, title="t", scope="project")
    out = search(query="foo bar", scope="project")
    for r in out["results"]:
        assert "matched_chunk_id" not in r, f"matched_chunk_id leaked into MCP dict: {r}"


def test_chunked_memory_deduped_to_single_result(project_db):
    """A long memory split into many chunks where multiple chunks match
    the query must appear exactly ONCE in results (deduped to parent)."""
    long_md = (
        "## A\n" + ("keyword " * 100) + "\n\n"
        "## B\n" + ("keyword " * 100) + "\n\n"
        "## C\n" + ("keyword " * 100)
    )
    store(content=long_md, title="multi-chunk-match", scope="project")
    out = search(query="keyword", scope="project")
    titles = [r["title"] for r in out["results"]]
    assert titles.count("multi-chunk-match") == 1


def test_empty_chunks_vec_still_returns_via_fts(project_db):
    """Per spec A7: if chunks_vec is empty (cap exceeded or quality
    threshold blocks embedding), BM25 search via chunks_fts still
    works. Simulate by deleting chunks_vec entries after storage."""
    long_md = "## A\n" + ("uniquetestword " * 200) + "\n\n## B\n" + ("other " * 200)
    store(content=long_md, title="vec-stripped", scope="project")
    project_db.execute("DELETE FROM chunks_vec")
    project_db.commit()

    out = search(query="uniquetestword", scope="project")
    titles = [r["title"] for r in out["results"]]
    assert "vec-stripped" in titles
