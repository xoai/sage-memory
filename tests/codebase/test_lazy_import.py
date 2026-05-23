"""T1 — lazy-import gate for the [codebase] extra.

Importing ``sage_memory.codebase`` MUST succeed whether or not the
``[codebase]`` extra (``tree-sitter-language-pack``) is installed.
``scan()`` itself MUST raise a clear ``RuntimeError`` with an install
hint when called without the extra.
"""

from __future__ import annotations

import sys

import pytest


def test_codebase_module_imports_cleanly():
    """The package itself never touches tree-sitter at import time."""
    import sage_memory.codebase as cb

    assert hasattr(cb, "scan")
    assert hasattr(cb, "ScanResult")


def test_scan_raises_runtime_error_when_extra_missing(monkeypatch):
    """With ``tree_sitter_language_pack`` unavailable, ``scan()`` MUST
    raise ``RuntimeError`` containing the install hint.

    Uses ``monkeypatch.setitem(sys.modules, ..., None)`` to disable the
    import without touching the on-disk package; teardown is automatic
    so subsequent tests see the original state (rev 3 Minor #5 hygiene).
    """
    from sage_memory import codebase as cb

    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)

    with pytest.raises(RuntimeError) as excinfo:
        cb.scan()

    msg = str(excinfo.value)
    assert "pip install 'sage-memory[codebase]'" in msg
    assert "[codebase] extra" in msg


def test_require_extra_helper_raises_when_missing(monkeypatch):
    """The internal gate is testable directly so future callers (CLI,
    MCP handler) can rely on the same primitive.
    """
    from sage_memory import codebase as cb

    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)

    with pytest.raises(RuntimeError, match="codebase"):
        cb._require_extra()
