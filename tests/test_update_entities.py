"""Task 4 — `sage_memory_update` with optional `entities` + `relations`.

REPLACE semantics per spec §`sage_memory_update`:
  - entities=None → no change to mentions/relations (today's behavior)
  - entities=[...] → DELETE all mentions + source-relations for this
    memory_id, then INSERT the new set
  - entities=[] → DELETE rows; no new ones written (empty REPLACE)
  - entities passed AND content changed → REPLACE, do NOT enqueue extract
  - entities=None AND content changed + LLM key → re-enqueue extract
"""

from __future__ import annotations

import pytest

from sage_memory import db as _db
from sage_memory.store import store, update


CONTENT = ("PaymentOrchestrator coordinates StripeGateway and "
           "LedgerService. This is long enough to trigger worker enqueue.")


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _db.override_project_root(tmp_path)
    _db.close_all()
    yield tmp_path
    _db.close_all()
    _db.override_project_root(None)


def _ent(conn): return conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
def _men(conn): return conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"]
def _rel(conn): return conn.execute("SELECT COUNT(*) AS n FROM relations").fetchone()["n"]
def _qcnt(conn, t="extract"):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = ? AND status = 'pending'", (t,),
    ).fetchone()["n"]


def _seed(monkeypatch):
    """Helper: store a memory with two entities + one relation, return id."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = store(
        content=CONTENT, title="seed", scope="project",
        entities=[{"name": "A", "type": "CONCEPT"},
                  {"name": "B", "type": "CONCEPT"}],
        relations=[{"from": "A", "to": "B", "rel": "depends_on"}],
    )
    assert r["success"]
    return r["id"]


# ───── REPLACE semantics ─────

def test_update_with_new_entities_replaces_existing(fresh_project, monkeypatch):
    mid = _seed(monkeypatch)
    conn = _db.get_db("project")
    assert _men(conn) == 2
    assert _rel(conn) == 1

    r = update(
        id=mid, scope="project",
        entities=[
            {"name": "C", "type": "TECHNOLOGY"},
            {"name": "D", "type": "TECHNOLOGY"},
            {"name": "E", "type": "PERSON"},
        ],
        relations=[
            {"from": "C", "to": "D", "rel": "depends_on"},
            {"from": "D", "to": "E", "rel": "assigned_to"} if False else
            {"from": "D", "to": "E", "rel": "relates_to"},
        ],
    )
    assert r["success"]
    # Old mentions for `mid` are gone; new ones in place
    assert _men(conn) == 3
    # Old source-relations for `mid` gone; new ones in place
    assert _rel(conn) == 2


def test_update_entities_empty_clears_all_rows(fresh_project, monkeypatch):
    """Empty explicit list REPLACEs by deleting rows; writes nothing
    new. Response is success:true (empty REPLACE is not an error)."""
    mid = _seed(monkeypatch)
    conn = _db.get_db("project")
    assert _men(conn) == 2

    r = update(id=mid, scope="project", entities=[], relations=[])
    assert r["success"]
    assert _men(conn) == 0
    assert _rel(conn) == 0


def test_update_entities_none_leaves_rows_alone(fresh_project, monkeypatch):
    """No `entities` arg → mentions/relations untouched (today's behavior)."""
    mid = _seed(monkeypatch)
    conn = _db.get_db("project")

    r = update(id=mid, scope="project", title="renamed")
    assert r["success"]
    # Untouched
    assert _men(conn) == 2
    assert _rel(conn) == 1


# ───── Interaction with content change + worker enqueue ─────

def test_update_with_entities_and_content_change_replaces_no_worker(
    fresh_project, monkeypatch,
):
    """When agent passes entities AND content changes, REPLACE happens
    and worker is NOT enqueued (the agent has provided the extraction)."""
    mid = _seed(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    conn = _db.get_db("project")

    r = update(
        id=mid, scope="project",
        content=CONTENT + " Plus a new section about NewService.",
        entities=[{"name": "NewService", "type": "CONCEPT"}],
        relations=[],
    )
    assert r["success"]
    # Old A/B mentions for this memory are gone
    n_old_mentions = conn.execute(
        "SELECT COUNT(*) AS n FROM mentions m "
        "JOIN entities e ON e.id = m.entity_id "
        "WHERE e.name IN ('A','B') AND m.memory_id = ?",
        (mid,),
    ).fetchone()["n"]
    assert n_old_mentions == 0
    # NewService mention is in place
    n_new = conn.execute(
        "SELECT COUNT(*) AS n FROM mentions m "
        "JOIN entities e ON e.id = m.entity_id "
        "WHERE e.name = 'NewService'",
    ).fetchone()["n"]
    assert n_new == 1
    # No worker enqueue
    assert _qcnt(conn, "extract") == 0


def test_update_entities_none_content_change_enqueues_extract(
    fresh_project, monkeypatch,
):
    """Today's behavior preserved: content change + LLM key + no
    entities → worker enqueued."""
    mid = _seed(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    conn = _db.get_db("project")

    r = update(
        id=mid, scope="project",
        content=CONTENT + " Plus more content here for the worker.",
    )
    assert r["success"]
    # Worker enqueued (today's behavior)
    assert _qcnt(conn, "extract") == 1
    # Old mentions/relations for this memory untouched (only worker can
    # change them, and it hasn't run yet)
    assert _men(conn) == 2


# ───── Validation ─────

def test_update_invalid_entity_rejects(fresh_project, monkeypatch):
    mid = _seed(monkeypatch)
    r = update(
        id=mid, scope="project",
        entities=[{"name": "X", "type": "NOT_A_TYPE"}],
    )
    assert not r["success"]


# ───── Error case: memory not found ─────

def test_update_not_found_returns_error(fresh_project):
    r = update(id="nonexistent", scope="project",
               entities=[{"name": "X", "type": "CONCEPT"}])
    assert not r["success"]
    assert "Not found" in r["message"]
