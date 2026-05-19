"""Task 3 — `sage_memory_store` with optional `entities` + `relations`.

Covers the new 0.9.0 agent-driven extraction path:
- Backwards-compat: `entities=None` + LLM key → worker enqueue (AC #2)
- Free-path: `entities=None` + no LLM key → no enqueue
- Agent path: `entities=[{...}]` → synchronous write, no enqueue
- Explicit suppression: `entities=[]` → no enqueue even with LLM key
- Validation errors → success:false, no rows written
"""

from __future__ import annotations

import pytest

from sage_memory import db as _db
from sage_memory.store import store


CONTENT = ("PaymentOrchestrator coordinates StripeGateway and "
           "LedgerService. This content is long enough to trigger "
           "the worker enqueue floor of 50 characters.")


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _db.override_project_root(tmp_path)
    _db.close_all()
    yield tmp_path
    _db.close_all()
    _db.override_project_root(None)


def _entity_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]


def _mention_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"]


def _relation_count(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM relations").fetchone()["n"]


def _queue_count(conn, task_type="extract"):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM extraction_queue "
        "WHERE task_type = ? AND status = 'pending'",
        (task_type,),
    ).fetchone()["n"]


# ───── AC #2: backwards-compat ─────

def test_entities_none_with_llm_key_enqueues_extract(fresh_project, monkeypatch):
    """Backwards-compat: when entities is not passed and an LLM key is
    configured, an `extract` task lands in the queue (today's behavior
    preserved)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(content=CONTENT, title="Payment saga", scope="project")
    assert result["success"]
    conn = _db.get_db("project")
    assert _queue_count(conn, "extract") == 1


def test_entities_none_without_llm_key_no_enqueue(fresh_project, monkeypatch):
    """Free-path: no LLM key + no entities → no enqueue (today's
    behavior preserved)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = store(content=CONTENT, title="Payment saga", scope="project")
    assert result["success"]
    conn = _db.get_db("project")
    assert _queue_count(conn, "extract") == 0


# ───── Agent path: synchronous write ─────

def test_entities_provided_writes_synchronously(fresh_project, monkeypatch):
    """Agent-provided entities are written into entities/mentions/
    relations tables synchronously, in the same transaction as the
    memory row. No worker enqueue."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(
        content=CONTENT,
        title="Payment saga",
        scope="project",
        entities=[
            {"name": "PaymentOrchestrator", "type": "CONCEPT"},
            {"name": "StripeGateway", "type": "CONCEPT"},
            {"name": "Stripe", "type": "TECHNOLOGY"},
        ],
        relations=[
            {"from": "PaymentOrchestrator", "to": "StripeGateway",
             "rel": "depends_on"},
            {"from": "StripeGateway", "to": "Stripe", "rel": "depends_on"},
        ],
    )
    assert result["success"]
    conn = _db.get_db("project")
    assert _entity_count(conn) == 3
    assert _mention_count(conn) == 3
    assert _relation_count(conn) == 2
    # Worker NOT enqueued — agent has done the extraction
    assert _queue_count(conn, "extract") == 0


def test_entities_empty_list_suppresses_worker(fresh_project, monkeypatch):
    """entities=[] (explicit empty) suppresses the worker enqueue even
    when an LLM key is set. Per spec: the agent has affirmatively
    decided this memory has no entities."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(
        content=CONTENT, title="t", scope="project",
        entities=[], relations=[],
    )
    assert result["success"]
    conn = _db.get_db("project")
    assert _queue_count(conn, "extract") == 0
    assert _entity_count(conn) == 0


# ───── Validation errors ─────

def test_invalid_entity_type_returns_error_no_rows_written(fresh_project, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(
        content=CONTENT, title="t", scope="project",
        entities=[{"name": "Bad", "type": "NOT_A_TYPE"}],
    )
    assert not result["success"]
    assert "NOT_A_TYPE" in result["message"] or "vocab" in result["message"]
    # Memory row NOT written either — validation rejects the whole call
    conn = _db.get_db("project")
    n_memories = conn.execute(
        "SELECT COUNT(*) AS n FROM memories"
    ).fetchone()["n"]
    assert n_memories == 0


def test_invalid_relation_returns_error_no_rows_written(fresh_project, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(
        content=CONTENT, title="t", scope="project",
        entities=[{"name": "A", "type": "CONCEPT"},
                  {"name": "B", "type": "CONCEPT"}],
        relations=[{"from": "A", "to": "B", "rel": "INVALID"}],
    )
    assert not result["success"]


def test_oversize_entities_rejected(fresh_project, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = store(
        content=CONTENT, title="t", scope="project",
        entities=[{"name": f"E{i}", "type": "CONCEPT"} for i in range(51)],
    )
    assert not result["success"]


# ───── Relation endpoint resolution ─────

def test_relation_with_unresolvable_endpoint_silently_dropped(
    fresh_project, monkeypatch,
):
    """A relation referencing a name not in entities (and not in DB) is
    silently dropped, but the store call still succeeds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = store(
        content=CONTENT, title="t", scope="project",
        entities=[{"name": "A", "type": "CONCEPT"}],
        relations=[{"from": "A", "to": "NonExistent", "rel": "depends_on"}],
    )
    assert result["success"]
    conn = _db.get_db("project")
    assert _entity_count(conn) == 1
    assert _relation_count(conn) == 0


# ───── Scope: project + global ─────

def test_entities_work_with_global_scope(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Use unique content so this test doesn't dedup against any
    # global-DB rows left by prior tests in the same session.
    import uuid as _uuid
    unique = f"Task 3 scope=global smoke {_uuid.uuid4().hex}: " + CONTENT
    result = store(
        content=unique, title="t", scope="global",
        entities=[{"name": f"Test_{_uuid.uuid4().hex[:8]}", "type": "CONCEPT"}],
    )
    assert result["success"]
    # Verify entity was written under global scope
    conn = _db.get_db("global")
    n_recent = conn.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE name LIKE 'Test_%'"
    ).fetchone()["n"]
    assert n_recent >= 1
