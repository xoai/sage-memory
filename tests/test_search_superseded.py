"""T3 — search.py `_annotate_superseded` integration.

When a `sage_memory_search` result has an incoming `supersedes`
edge, the envelope carries `superseded_by: <newer_id>`. Results
are NOT filtered or down-ranked — agents and humans decide.

`_annotate_superseded` is called per-DB after the final dedup
loop. Cross-DB supersedes relations are NOT followed (the `edges`
table is per-DB).
"""

from __future__ import annotations

import time
import uuid

import pytest

from sage_memory.db import (
    _open, close_all, get_project_db_path, override_project_root,
)
from sage_memory.embedder import (
    EMBEDDING_DIM, LocalEmbedder, set_embedder,
)
import sage_memory.db as _db_mod
import sage_memory.search as _search_mod
from sage_memory.search import search


# ─── Fixture: project + global DBs ────────────────────────────────


@pytest.fixture
def project_and_global_db(tmp_path, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".git").mkdir()
    close_all()
    override_project_root(tmp_path)
    set_embedder(LocalEmbedder())
    proj = _open(get_project_db_path(tmp_path))
    glob = _open(tmp_path / "global_test.db")

    def fake_get_global_db():
        return glob

    def fake_get_all_dbs():
        return [("project", proj), ("global", glob)]

    monkeypatch.setattr(_db_mod, "get_global_db", fake_get_global_db)
    monkeypatch.setattr(_db_mod, "get_all_dbs", fake_get_all_dbs)
    monkeypatch.setattr(_search_mod, "get_all_dbs", fake_get_all_dbs)

    yield proj, glob
    close_all()


def _insert(db, *, mid, title, content, content_hash=None):
    now = time.time()
    if content_hash is None:
        content_hash = f"hash_{mid}"
    db.execute(
        """INSERT INTO memories
           (id, title, content, tags, content_hash, embedded, status,
            created_at, updated_at, accessed_at, access_count)
           VALUES (?, ?, ?, '[]', ?, 0, 'active', ?, ?, ?, 0)""",
        (mid, title, content, content_hash, now, now, now),
    )
    db.commit()


def _link(db, *, source_id, target_id, relation="supersedes", created_at=None):
    edge_id = uuid.uuid4().hex
    if created_at is None:
        created_at = time.time()
    db.execute(
        """INSERT INTO edges (id, source_id, target_id, relation, properties, created_at)
           VALUES (?, ?, ?, ?, '{}', ?)""",
        (edge_id, source_id, target_id, relation, created_at),
    )
    db.commit()


# ─── (1) Annotation present when incoming supersedes exists ───────


def test_supersedes_annotation_present_when_incoming_edge_exists(
    project_and_global_db,
):
    proj, _ = project_and_global_db
    _insert(proj, mid="M_old", title="old payment doc",
            content="payment processing details for the old API")
    _insert(proj, mid="M_new", title="new payment doc",
            content="payment processing details for the new API")
    _link(proj, source_id="M_new", target_id="M_old")  # M_new supersedes M_old

    res = search(query="payment processing", limit=5)
    by_id = {r["id"]: r for r in res["results"]}
    assert "M_old" in by_id
    assert by_id["M_old"].get("superseded_by") == "M_new"


# ─── (2) No annotation when no incoming edge ──────────────────────


def test_no_annotation_when_no_incoming_supersedes(project_and_global_db):
    proj, _ = project_and_global_db
    _insert(proj, mid="M_solo", title="solo doc",
            content="standalone documentation about authentication tokens")

    res = search(query="authentication tokens", limit=5)
    by_id = {r["id"]: r for r in res["results"]}
    assert "M_solo" in by_id
    assert "superseded_by" not in by_id["M_solo"]


# ─── (3) Multiple incoming supersedes → most recent wins ──────────


def test_multiple_incoming_supersedes_most_recent_wins(project_and_global_db):
    proj, _ = project_and_global_db
    _insert(proj, mid="M_v1", title="v1", content="rate limiting strategy version 1")
    _insert(proj, mid="M_v2", title="v2", content="rate limiting strategy version 2")
    _insert(proj, mid="M_v3", title="v3", content="rate limiting strategy version 3")

    # M_v2 supersedes M_v1 at t=1000; M_v3 supersedes M_v1 at t=2000.
    _link(proj, source_id="M_v2", target_id="M_v1", created_at=1000.0)
    _link(proj, source_id="M_v3", target_id="M_v1", created_at=2000.0)

    res = search(query="rate limiting strategy", limit=5)
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["M_v1"].get("superseded_by") == "M_v3", (
        f"expected most-recent supersedes (M_v3) to win, got "
        f"{by_id['M_v1'].get('superseded_by')!r}"
    )


# ─── (4) Outgoing supersedes does NOT annotate ────────────────────


def test_outgoing_supersedes_does_not_annotate_source(project_and_global_db):
    proj, _ = project_and_global_db
    _insert(proj, mid="M_old", title="old", content="caching policy old documentation")
    _insert(proj, mid="M_new", title="new", content="caching policy new documentation")
    _link(proj, source_id="M_new", target_id="M_old")  # M_new IS the newer

    res = search(query="caching policy", limit=5)
    by_id = {r["id"]: r for r in res["results"]}
    assert "M_new" in by_id
    assert "superseded_by" not in by_id["M_new"], (
        "M_new is the source (newer) of supersedes — should NOT carry "
        "superseded_by; only the target (older) gets the annotation"
    )


# ─── (5) Annotation doesn't change result position ────────────────


def test_supersedes_annotation_does_not_change_result_position(
    project_and_global_db,
):
    proj, _ = project_and_global_db
    # Three memories; M_top has the most keyword overlap so it ranks first.
    _insert(proj, mid="M_top", title="top",
            content="webhook delivery webhook delivery webhook delivery semantics")
    _insert(proj, mid="M_mid", title="mid", content="webhook delivery semantics overview")
    _insert(proj, mid="M_low", title="low", content="webhook config low priority")
    _insert(proj, mid="M_new_top",
            title="new top", content="updated webhook delivery semantics")
    _link(proj, source_id="M_new_top", target_id="M_top")

    res = search(query="webhook delivery semantics", limit=5)
    ids = [r["id"] for r in res["results"]]
    # M_top should still be in the top-2 (highly keyword-matched);
    # annotation shouldn't have moved it.
    assert "M_top" in ids[:3]
    # And its annotation IS set.
    by_id = {r["id"]: r for r in res["results"]}
    assert by_id["M_top"].get("superseded_by") == "M_new_top"


# ─── (6) Empty result list — no crash ─────────────────────────────


def test_empty_result_list_no_crash(project_and_global_db):
    """Search for content that matches nothing; annotation must
    handle the empty list without error."""
    proj, _ = project_and_global_db
    _insert(proj, mid="M_a", title="a", content="completely unrelated content here")

    res = search(query="xyzqq_no_match_keyword_blob", limit=5)
    # Either empty or just stray matches — but no crash.
    assert isinstance(res["results"], list)


# ─── (7) Cross-DB scenario — annotation runs per-DB ───────────────


def test_supersedes_annotation_runs_per_db(project_and_global_db):
    """Project memory has incoming supersedes (annotated). Global
    memory has no incoming supersedes (NOT annotated). Cross-DB
    edge lookup doesn't leak: a project-DB supersedes edge does
    not annotate a global-scope result, and vice versa."""
    proj, glob = project_and_global_db
    # Project: M_p_old superseded by M_p_new
    _insert(proj, mid="M_p_old", title="project old",
            content="oauth token rotation handling in the project doc")
    _insert(proj, mid="M_p_new", title="project new",
            content="oauth token rotation handling improved project doc")
    _link(proj, source_id="M_p_new", target_id="M_p_old")
    # Global: M_g_solo with no edges (different content_hash than
    # M_p_old so dedup doesn't collapse them).
    _insert(glob, mid="M_g_solo", title="global solo",
            content="oauth token rotation handling for global notes",
            content_hash="hash_global_distinct")

    res = search(query="oauth token rotation", limit=10)
    by_id = {r["id"]: r for r in res["results"]}
    # Both should surface.
    assert "M_p_old" in by_id
    assert "M_g_solo" in by_id
    # Project-scope memory: annotated.
    assert by_id["M_p_old"].get("superseded_by") == "M_p_new"
    # Global-scope memory: NOT annotated (no edge in global DB).
    assert "superseded_by" not in by_id["M_g_solo"], (
        f"global memory falsely annotated: {by_id['M_g_solo']}"
    )
