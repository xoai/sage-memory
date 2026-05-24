"""T6 — end-to-end integration: store → link → search round-trip.

Exercises the complete 0.12.0 semantic-dedup flow via the public
Python API:
  1. Store memory A.
  2. Store memory B (verified paraphrase of A) →
     `suggested_links` carries `confidence: "near_duplicate"`
     pointing at A.
  3. Link B → A via `relation: "supersedes"`.
  4. Search → both A and B surface; A carries
     `superseded_by: <B.id>`; B does NOT.

Uses FastEmbedder — the only embedder in our stack that produces
≥ 0.95 cosine on the test paraphrase pair under bge-small-en-v1.5.
"""

from __future__ import annotations

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    FastEmbedder, LocalEmbedder, set_embedder,
)
import sage_memory.db as _db_mod
import sage_memory.search as _search_mod


@pytest.fixture
def project_db_fastembed_e2e(tmp_path, monkeypatch):
    """Project + global DBs with FastEmbedder active."""
    pytest.importorskip("fastembed")
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(FastEmbedder())
    proj = _open(get_project_db_path(tmp_path))
    glob = _open(tmp_path / "global_test.db")

    def fake_get_global_db():
        return glob

    def fake_get_all_dbs():
        return [("project", proj), ("global", glob)]

    monkeypatch.setattr(_db_mod, "get_global_db", fake_get_global_db)
    monkeypatch.setattr(_db_mod, "get_all_dbs", fake_get_all_dbs)
    monkeypatch.setattr(_search_mod, "get_all_dbs", fake_get_all_dbs)

    yield proj
    close_all()
    set_embedder(LocalEmbedder())


def test_store_link_search_round_trip(project_db_fastembed_e2e):
    from sage_memory.store import store
    from sage_memory.graph import link
    from sage_memory.search import search

    a_text = (
        "PaymentOrchestrator uses the saga pattern for distributed transactions"
    )
    b_text = (
        "PaymentOrchestrator implements distributed transactions via the saga pattern"
    )

    # (1) Store A.
    a_res = store(content=a_text, title="A: payment saga (original)")
    assert a_res["success"], a_res

    # (2) Store B — paraphrase → suggested_links carries near_duplicate.
    b_res = store(content=b_text, title="B: payment saga (paraphrase)")
    assert b_res["success"], b_res
    near_dups = [
        e for e in b_res.get("suggested_links", [])
        if e.get("confidence") == "near_duplicate"
    ]
    assert len(near_dups) >= 1, (
        f"expected near_duplicate signal in suggested_links for B; got {b_res}"
    )
    assert any(e["target_id"] == a_res["id"] for e in near_dups)
    # Sanity on the entry shape.
    nd = next(e for e in near_dups if e["target_id"] == a_res["id"])
    assert nd["confidence"] == "near_duplicate"
    assert nd["similarity"] >= 0.95
    assert ">=" in nd["reason"]   # ASCII >=, not Unicode ≥

    # (3) Link B → A via supersedes.
    link_res = link(
        source_id=b_res["id"], target_id=a_res["id"], relation="supersedes",
    )
    assert link_res["success"], link_res

    # (4) Search returns both; A carries superseded_by=B; B does not.
    s_res = search(query="payment saga distributed transactions", limit=10)
    by_id = {r["id"]: r for r in s_res["results"]}
    assert a_res["id"] in by_id, f"A missing from search results: {s_res}"
    assert b_res["id"] in by_id, f"B missing from search results: {s_res}"
    assert by_id[a_res["id"]].get("superseded_by") == b_res["id"], (
        f"A not annotated with superseded_by=B; got "
        f"{by_id[a_res['id']]}"
    )
    assert "superseded_by" not in by_id[b_res["id"]], (
        f"B (newer) incorrectly annotated: {by_id[b_res['id']]}"
    )
