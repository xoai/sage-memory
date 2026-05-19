"""Task 6 — `suggested_links` field in store/update responses.

Per spec §`sage_memory_store` response: every store/update returns
`suggested_links` as a list (empty when no matches). Covered in the
MCP wire format by the existing JSON pass-through in server.py.
"""

from __future__ import annotations

import pytest

from sage_memory import db as _db
from sage_memory.store import store, update


@pytest.fixture
def fresh_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _db.override_project_root(tmp_path)
    _db.close_all()
    yield tmp_path
    _db.close_all()
    _db.override_project_root(None)


CONTENT_LONG = (
    "PaymentOrchestrator coordinates StripeGateway and LedgerService. "
    "This entry discusses the saga pattern in the checkout flow."
)


def test_store_response_includes_suggested_links_field(
    fresh_project, monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = store(content=CONTENT_LONG, title="t", scope="project")
    assert "suggested_links" in result
    # Empty DB at this point → no matches
    assert result["suggested_links"] == []


def test_store_response_populated_when_match_exists(
    fresh_project, monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    first = store(
        content="PaymentOrchestrator coordinates the checkout flow with "
                "StripeGateway in the billing service architecture.",
        title="[Task:task_a1b2] Fix payment timeout in checkout flow",
        scope="project",
    )
    # Second store with related content
    second = store(
        content="Discovered N+1 query in ReportBuilder causing payment "
                "timeout in the checkout flow during peak hours.",
        title="N+1 perf issue", scope="project",
    )
    assert "suggested_links" in second
    suggestions = second["suggested_links"]
    assert isinstance(suggestions, list)
    assert any(s["target_id"] == first["id"] for s in suggestions)
    # Each entry has the documented shape
    for s in suggestions:
        assert set(s.keys()) >= {"target_id", "target_title", "reason"}


def test_update_response_includes_suggested_links_field(
    fresh_project, monkeypatch,
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = store(content=CONTENT_LONG, title="t", scope="project")
    out = update(id=r["id"], scope="project", title="renamed-title")
    assert "suggested_links" in out
    assert isinstance(out["suggested_links"], list)


def test_store_failure_response_does_not_require_suggested_links(
    fresh_project, monkeypatch,
):
    """Validation-error responses don't include a `suggested_links`
    field (or it's safely absent) — the call rejected before any
    lookup happened."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = store(
        content="too short", title="t", scope="project",
    )
    assert not result["success"]
    # No assertion that the field exists on failure — it's an OK
    # implementation detail to omit. But field, if present, must be
    # a list.
    if "suggested_links" in result:
        assert isinstance(result["suggested_links"], list)
