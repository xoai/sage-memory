"""T10a — ``sage_memory_scan_codebase`` MCP tool surface tests.

Three layers of coverage:

1. **Tool registry**: the tool is in TOOLS and HANDLERS regardless of
   whether the ``[codebase]`` extra is installed (agents see a stable
   surface; the failure-on-call envelope handles missing extras).
2. **Missing-extra envelope**: shape is ``{success: false,
   message: str}`` per spec rev 2 — NOT the older
   ``{success, error, install_hint}`` shape.
3. **Success envelope**: contains the ``files``, ``symbols``,
   ``relations`` sub-objects per spec line 251; ``files.with_error_nodes``
   is always present (even when zero).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage_memory.server import TOOLS, HANDLERS, scan_codebase


# ---------------------------------------------------------------------------
# Tool registry (stable surface — runs without the extra)
# ---------------------------------------------------------------------------


def test_tool_registered_unconditionally() -> None:
    """``sage_memory_scan_codebase`` appears in TOOLS regardless of
    the [codebase] extra install state. Spec rev 2 §"MCP discoverability"
    line 282-285.
    """
    names = [t.name for t in TOOLS]
    assert "sage_memory_scan_codebase" in names


def test_tool_handler_registered() -> None:
    assert "sage_memory_scan_codebase" in HANDLERS
    assert HANDLERS["sage_memory_scan_codebase"] is scan_codebase


def test_tool_description_mentions_extra() -> None:
    tool = next(t for t in TOOLS if t.name == "sage_memory_scan_codebase")
    # Spec rev 2 §"MCP discoverability" line 283-284: description must
    # explicitly state the extra requirement so agents check install
    # state before invoking.
    assert "[codebase]" in tool.description
    assert "extra" in tool.description
    assert "pip install" in tool.description


def test_tool_input_schema_has_all_flags() -> None:
    tool = next(t for t in TOOLS if t.name == "sage_memory_scan_codebase")
    schema = tool.inputSchema
    assert schema["type"] == "object"
    props = schema["properties"]
    # All 6 CLI flags surface as MCP input properties.
    for field in ("path", "languages", "include_ignored",
                  "limit", "force", "dry_run"):
        assert field in props, f"missing input property: {field}"


# ---------------------------------------------------------------------------
# Missing-extra envelope shape (rev 2 alignment)
# ---------------------------------------------------------------------------


def test_missing_extra_envelope_shape(monkeypatch) -> None:
    """The failure envelope must be ``{success: false, message: str}``
    — NOT the older ``{success, error, install_hint}`` form. Verified
    by making the inner ``scan()`` raise the install-hint RuntimeError
    (matches what ``_require_extra`` raises when tree_sitter_language_pack
    isn't installed).
    """
    import sage_memory.codebase as codebase_mod

    def fake_scan(*args, **kwargs):
        raise RuntimeError(
            "scan-codebase requires the [codebase] extra:\n"
            "  pip install 'sage-memory[codebase]'"
        )

    monkeypatch.setattr(codebase_mod, "scan", fake_scan)

    result = scan_codebase(path=None)
    assert result["success"] is False
    assert isinstance(result["message"], str)
    assert "[codebase] extra" in result["message"]
    # The older keys must NOT be present.
    assert "error" not in result
    assert "install_hint" not in result


# NOTE: The `try: from .codebase import scan` branch in the MCP
# handler is defensive against an exotic broken install where the
# codebase package itself fails to import. In a real missing-extra
# scenario the codebase module imports cleanly (only the tree-sitter
# import inside _require_extra() fails) so the ImportError branch is
# functionally dead code under normal installs — we don't write a
# dedicated test for it.


# ---------------------------------------------------------------------------
# Success envelope shape
# ---------------------------------------------------------------------------


tsp = pytest.importorskip("tree_sitter_language_pack")


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch):
    from sage_memory.db import close_all
    close_all()
    monkeypatch.setenv("SAGE_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text("[tool.test]\n")
    yield tmp_path
    close_all()


def test_success_envelope_shape(isolated_project) -> None:
    """Spec line 251-258 shape:
        {success, files: {scanned, changed, unchanged, with_error_nodes},
         symbols: {...}, relations: {imports, calls_resolved,
         calls_unresolved}, elapsed_ms}
    """
    (isolated_project / "main.py").write_text(
        "def helper(): return 1\n"
        "def caller(): return helper()\n"
    )
    result = scan_codebase(path=str(isolated_project))
    assert result["success"] is True
    assert "files" in result
    assert set(result["files"].keys()) >= {
        "scanned", "changed", "unchanged", "with_error_nodes",
    }
    assert "symbols" in result
    assert "relations" in result
    assert set(result["relations"].keys()) >= {
        "imports", "calls_resolved", "calls_unresolved",
    }
    assert "elapsed_ms" in result
    assert isinstance(result["elapsed_ms"], int)


def test_success_with_error_nodes_always_present(isolated_project) -> None:
    """Spec line 261: ``files.with_error_nodes`` is ALWAYS present
    in the envelope, value 0 when no parse errors occurred.
    """
    (isolated_project / "main.py").write_text("def f(): pass\n")
    result = scan_codebase(path=str(isolated_project))
    assert "with_error_nodes" in result["files"]
    assert result["files"]["with_error_nodes"] == 0


def test_success_files_counters_consistent(isolated_project) -> None:
    """``scanned == changed + unchanged`` after the first scan and
    after a no-op re-scan.
    """
    (isolated_project / "a.py").write_text("def x(): pass\n")
    (isolated_project / "b.py").write_text("def y(): pass\n")

    first = scan_codebase(path=str(isolated_project))
    assert first["files"]["scanned"] == 2
    assert first["files"]["changed"] == 2
    assert first["files"]["unchanged"] == 0

    second = scan_codebase(path=str(isolated_project))
    assert second["files"]["scanned"] == 2
    assert second["files"]["changed"] == 0
    assert second["files"]["unchanged"] == 2


def test_dry_run_envelope(isolated_project) -> None:
    (isolated_project / "a.py").write_text("def x(): pass\n")
    result = scan_codebase(path=str(isolated_project), dry_run=True)
    assert result["success"] is True
    assert result["dry_run"] is True
    # In dry-run, files.scanned is the WOULD-BE count; no other
    # counters are populated (changed/unchanged stay 0).
    assert result["files"]["scanned"] == 1
    assert result["files"]["changed"] == 0
    assert result["files"]["unchanged"] == 0


def test_symbols_dict_excludes_synthetic_module_anchor(isolated_project) -> None:
    """The synthetic ``<module>`` anchor (kind=MODULE) created during
    resolve must NOT appear in the symbols summary — agents reading
    ``symbols.MODULE: N`` would be confused by an internal artifact.
    """
    (isolated_project / "main.py").write_text(
        'import os\n'
        '\n'
        'def caller(): return 1\n'
    )
    result = scan_codebase(path=str(isolated_project))
    assert "MODULE" not in result["symbols"]
