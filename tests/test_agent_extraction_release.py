"""Task 10 — 0.9.0 release acceptance: version, CHANGELOG, README,
MCP tool descriptions.

Asserts the user-facing surface advertises the new behavior. Not a
behavioral test — those live in the per-task test files.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_pyproject_carries_a_version():
    """pyproject.toml has SOME version. Exact version asserted by the
    active release cycle's own acceptance test (0.10.0 lives in
    test_skill_rename_release.py); this check is a stale-file smoke."""
    text = (REPO / "pyproject.toml").read_text()
    import re
    assert re.search(r'version = "\d+\.\d+\.\d+"', text)


def test_package_version_resolves_to_a_version():
    import sage_memory
    import re
    assert re.match(r"^\d+\.\d+\.\d+", sage_memory.__version__)


def test_changelog_has_090_entry():
    text = (REPO / "CHANGELOG.md").read_text()
    assert "## [0.9.0]" in text
    section = text.split("## [0.9.0]", 1)[1].split("\n## ", 1)[0]
    # Must cover all 4 user-facing changes called out in the spec
    assert "entities" in section and "relations" in section
    assert "suggested_links" in section
    assert "expand" in section and "rerank" in section
    assert "Deprecated" in section
    assert "install-skills" in section


def test_readme_documents_agent_driven_extraction():
    text = (REPO / "README.md").read_text()
    assert "Agent-driven extraction" in text
    assert "entities" in text and "relations" in text


def test_mcp_store_tool_description_mentions_entities():
    from sage_memory.server import TOOLS
    store_tool = next(t for t in TOOLS if t.name == "sage_memory_store")
    assert "entities" in store_tool.description
    # Schema declares the new properties
    props = store_tool.inputSchema["properties"]
    assert "entities" in props
    assert "relations" in props


def test_mcp_update_tool_description_mentions_entities():
    from sage_memory.server import TOOLS
    update_tool = next(t for t in TOOLS if t.name == "sage_memory_update")
    assert "entities" in update_tool.description
    props = update_tool.inputSchema["properties"]
    assert "entities" in props
    assert "relations" in props


def test_mcp_search_schema_declares_false_defaults():
    """Search tool schema declares `default: false` for the LLM stages
    (matches the implementation flip in Task 7)."""
    from sage_memory.server import TOOLS
    search_tool = next(t for t in TOOLS if t.name == "sage_memory_search")
    props = search_tool.inputSchema["properties"]
    assert props["expand"].get("default") is False
    assert props["rerank"].get("default") is False
