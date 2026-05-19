"""Task 7 — `sage_memory_search` default flip for `expand` / `rerank`.

Per 0.9.0 spec §`sage_memory_search`: defaults for `expand` and
`rerank` flip from `None`-resolves-to-`llm.is_configured()` to
explicit `False`. Re-enable requires `expand=True` / `rerank=True`.
"""

from __future__ import annotations

from sage_memory.search import _resolve_llm_stage_enabled


# ───── Default flip ─────

def test_param_none_returns_false_regardless_of_llm_key(monkeypatch):
    """The headline behavior change: param=None → False, NOT
    llm.is_configured()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _resolve_llm_stage_enabled("expand", None) is False
    assert _resolve_llm_stage_enabled("rerank", None) is False

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _resolve_llm_stage_enabled("expand", None) is False
    assert _resolve_llm_stage_enabled("rerank", None) is False


# ───── Explicit True/False still honored ─────

def test_param_true_with_llm_key_enables(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _resolve_llm_stage_enabled("expand", True) is True
    assert _resolve_llm_stage_enabled("rerank", True) is True


def test_param_true_without_llm_key_warns_returns_false(monkeypatch, caplog):
    """Explicit True with no key → log warning + return False (existing
    behavior preserved for the True case)."""
    import logging
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING):
        result = _resolve_llm_stage_enabled("expand", True)
    assert result is False
    assert any("no LLM key" in r.message for r in caplog.records)


def test_param_false_skips_silently(monkeypatch):
    """Explicit False → False, regardless of LLM key state."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _resolve_llm_stage_enabled("expand", False) is False
    assert _resolve_llm_stage_enabled("rerank", False) is False


# ───── MCP wire-format default ─────

def test_mcp_search_tool_inputschema_defaults_to_false():
    """The MCP `sage_memory_search` tool's inputSchema must declare
    `default: false` for `expand` and `rerank` so clients see the
    documented default."""
    from sage_memory.server import TOOLS
    search_tool = next(t for t in TOOLS if t.name == "sage_memory_search")
    schema = search_tool.inputSchema
    expand = schema["properties"]["expand"]
    rerank = schema["properties"]["rerank"]
    assert expand.get("default") is False, (
        f"expand default should be False; got {expand.get('default')!r}"
    )
    assert rerank.get("default") is False, (
        f"rerank default should be False; got {rerank.get('default')!r}"
    )
