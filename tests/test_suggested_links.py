"""Task 5 — `suggested_links.py` functional tests.

Returns up to 3 candidate link targets via direct FTS5 query (NOT
full `search()`). Status filter applied. No tag restriction. Perf
test is in `test_suggested_links_perf.py`.
"""

from __future__ import annotations

import pytest

from sage_memory import db as _db
from sage_memory.store import store
from sage_memory.suggested_links import find_suggested_links


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _db.override_project_root(tmp_path)
    _db.close_all()
    yield tmp_path
    _db.close_all()
    _db.override_project_root(None)


# ───── Empty / degenerate inputs ─────

def test_empty_db_returns_empty_list(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = _db.get_db("project")
    assert find_suggested_links(conn, "anything") == []


def test_short_content_returns_empty(fresh_project, monkeypatch):
    """Content < 20 chars short-circuits — no useful tokens."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = _db.get_db("project")
    assert find_suggested_links(conn, "tiny") == []


def test_stopword_only_content_returns_empty(fresh_project, monkeypatch):
    """Content with only stopwords/punctuation after tokenization → []."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Seed some content so DB isn't empty
    store(content="Some long content about Stripe payment processing for testing.",
          title="t", scope="project")
    conn = _db.get_db("project")
    # Content that produces empty FTS query (all stopwords / too short)
    result = find_suggested_links(conn, "the and the the the for of to or in")
    assert result == []


# ───── Match path ─────

def test_match_returns_target_id_title_reason(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = store(
        content="The PaymentOrchestrator service coordinates Stripe and "
                "LedgerService in the checkout flow.",
        title="[Task:task_a1b2] Fix payment timeout in checkout flow",
        scope="project",
    )
    target_id = r["id"]
    conn = _db.get_db("project")

    # New content mentions "payment timeout"
    out = find_suggested_links(
        conn,
        "Discovered N+1 query in ReportBuilder causing payment timeout "
        "during checkout. PaymentOrchestrator needs prefetch_related.",
    )
    assert len(out) >= 1
    assert any(s["target_id"] == target_id for s in out)
    found = next(s for s in out if s["target_id"] == target_id)
    assert "[Task:task_a1b2]" in found["target_title"]
    assert "match" in found["reason"].lower()


def test_limit_caps_at_3_by_default(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Seed 5 memories that all match a common token
    for i in range(5):
        store(
            content=f"Memory {i}: PaymentOrchestrator notes for entry "
                    f"number {i} discussing payment processing flow.",
            title=f"Payment topic {i}", scope="project",
        )
    conn = _db.get_db("project")
    out = find_suggested_links(
        conn,
        "PaymentOrchestrator processing for the payment flow analysis.",
    )
    assert len(out) <= 3


def test_explicit_limit_respected(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for i in range(5):
        store(
            content=f"Stripe webhook handling routine number {i} for "
                    f"payment processing pipeline notes.",
            title=f"Stripe note {i}", scope="project",
        )
    conn = _db.get_db("project")
    out = find_suggested_links(
        conn, "Stripe payment webhook investigation notes.",
        limit=2,
    )
    assert len(out) <= 2


# ───── Status filter ─────

def test_invalidated_entries_excluded(fresh_project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from sage_memory.store import update
    r = store(
        content="PaymentOrchestrator coordinates Stripe and ledger services "
                "for the checkout pipeline workflow.",
        title="Payment topic", scope="project",
    )
    update(id=r["id"], scope="project", status="invalidated")
    conn = _db.get_db("project")
    out = find_suggested_links(
        conn,
        "PaymentOrchestrator notes for checkout payment processing flow.",
    )
    # The only matching memory is invalidated → should not surface
    assert all(s["target_id"] != r["id"] for s in out)
