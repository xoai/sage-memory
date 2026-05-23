"""T9a — Python resolver + resolve_codebase orchestration.

Each test sets up a tiny Python project in tmp_path, runs T5's
``_scan_file`` over each file to populate ``code_symbols`` +
``codebase_scans``, then runs ``resolve_codebase`` and asserts the
shape of the ``code_relations`` rows.

Coverage per plan rev 3 T9a line 90:
- Same-file call resolves to a same-file symbol.
- Cross-file ``from a.b import foo`` resolves the import row to the
  ``foo`` symbol in module ``a.b``.
- Aliased import (``import numpy as np; np.array()``) → call row
  with target_name='np.array' and ``confidence='unresolved'``.
- Wildcard import (``from foo import *; bar()``) → bar() call
  unresolved (no symbol "bar" reachable in the source file).
- ``self.foo()`` resolves when the source method's enclosing class
  has a matching method; unresolved otherwise.
- Unresolved rows are inserted (not silently dropped) with
  ``confidence='unresolved'``.
- Two callsites at the same line / different columns land as
  distinct rows (UNIQUE discriminator E2E).

Module-level relations (``source_qname=""``) are NOT written to
``code_relations`` because that table's source_symbol_id is NOT NULL;
fixtures avoid relying on those for assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import _scan_file
from sage_memory.codebase._resolve import resolve_codebase
from sage_memory.db import _migrate


ALL_MIGRATIONS = (
    "001_initial.sql",
    "002_edges.sql",
    "003_memory_health.sql",
    "004_chunks.sql",
    "005_entities.sql",
    "006_embedding_meta.sql",
    "007_extraction_queue.sql",
    "008_worker_state.sql",
    "009_code_symbols.sql",
)


@pytest.fixture
def migrated_db(fresh_db, tmp_migrations_dir, copy_production_migrations):
    copy_production_migrations(*ALL_MIGRATIONS)
    _migrate(fresh_db, migrations_dir=tmp_migrations_dir)
    return fresh_db


@pytest.fixture
def py_parser():
    return tsp.get_parser("python")


def _scan_project(conn, project_root: Path, parser) -> None:
    """Scan every .py file under project_root via _scan_file."""
    for path in sorted(project_root.rglob("*.py")):
        rel = str(path.relative_to(project_root))
        _scan_file(
            conn,
            abs_path=path,
            rel_path=rel,
            language_tag="py",
            query_id="py",
            parser=parser,
            force=False,
        )


def _relations(conn) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT source_symbol_id, target_symbol_id, target_name, "
            "kind, confidence, line, column_start FROM code_relations"
        )
    ]


def _symbol_id(conn, qualified_name: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM code_symbols WHERE qualified_name = ? LIMIT 1",
        (qualified_name,),
    ).fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Same-file resolution
# ---------------------------------------------------------------------------


def test_same_file_call_resolves_to_same_file_symbol(
    migrated_db, py_parser, tmp_path,
) -> None:
    """A call ``helper(1)`` inside a method must resolve to the
    file-local ``helper`` FUNCTION row.
    """
    (tmp_path / "main.py").write_text(
        "def helper(x):\n"
        "    return x + 1\n"
        "\n"
        "class C:\n"
        "    def f(self):\n"
        "        return helper(1)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    result = resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )
    assert result.relations_resolved >= 1

    helper_id = _symbol_id(migrated_db, "helper")
    f_id = _symbol_id(migrated_db, "C.f")
    assert helper_id and f_id

    rel = next(
        r for r in _relations(migrated_db)
        if r["target_name"] == "helper" and r["source_symbol_id"] == f_id
    )
    assert rel["target_symbol_id"] == helper_id
    assert rel["confidence"] == "resolved"


# ---------------------------------------------------------------------------
# Cross-file Python import resolution
# ---------------------------------------------------------------------------


def test_cross_file_from_import_resolves(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``from pkg.util import helper`` in main.py — the import row
    must resolve to the ``helper`` FUNCTION symbol declared in
    pkg/util.py.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text(
        "def helper(x):\n    return x + 1\n"
    )
    (tmp_path / "main.py").write_text(
        "from pkg.util import helper\n"
        "\n"
        "def caller():\n"
        "    return helper(1)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    helper_id = _symbol_id(migrated_db, "helper")
    caller_id = _symbol_id(migrated_db, "caller")
    assert helper_id and caller_id

    import_rel = next(
        r for r in _relations(migrated_db)
        if r["kind"] == "imports" and r["target_name"] == "pkg.util.helper"
    )
    assert import_rel["target_symbol_id"] == helper_id
    assert import_rel["confidence"] == "resolved"
    # The import row's SOURCE is the caller (only non-module symbol
    # in main.py); module-level imports are not anchored to a symbol
    # and thus not recorded — see _resolve.py module docstring.


def test_cross_file_init_py_resolves(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``from pkg import helper`` where pkg/__init__.py exports
    ``helper`` resolves to that symbol — ``__init__`` is stripped
    when computing module_path.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def helper(x):\n    return x\n"
    )
    (tmp_path / "main.py").write_text(
        "from pkg import helper\n"
        "\n"
        "def caller():\n"
        "    return helper(1)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    helper_id = _symbol_id(migrated_db, "helper")
    rel = next(
        r for r in _relations(migrated_db)
        if r["kind"] == "imports" and r["target_name"] == "pkg.helper"
    )
    assert rel["target_symbol_id"] == helper_id


# ---------------------------------------------------------------------------
# Aliased imports → unresolved
# ---------------------------------------------------------------------------


def test_aliased_import_call_is_unresolved(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``import numpy as np; np.array()`` produces a call row with
    target_name='np.array' and confidence='unresolved' — the resolver
    has no alias map and can't recognize ``np`` as ``numpy``.
    """
    (tmp_path / "main.py").write_text(
        "import numpy as np\n"
        "\n"
        "def use_np():\n"
        "    return np.array(1)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    call = next(
        r for r in _relations(migrated_db)
        if r["kind"] == "calls" and r["target_name"] == "np.array"
    )
    assert call["target_symbol_id"] is None
    assert call["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Wildcard import → bar() call unresolved
# ---------------------------------------------------------------------------


def test_wildcard_import_call_is_unresolved(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``from foo import *; def caller(): bar()`` — the wildcard
    doesn't tell the resolver where ``bar`` comes from, so the call
    stays unresolved (and is still inserted as a row).
    """
    (tmp_path / "main.py").write_text(
        "from foo import *\n"
        "\n"
        "def caller():\n"
        "    return bar()\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    rels = [
        r for r in _relations(migrated_db)
        if r["kind"] == "calls" and r["target_name"] == "bar"
    ]
    assert len(rels) == 1
    assert rels[0]["target_symbol_id"] is None
    assert rels[0]["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# self.foo() resolution
# ---------------------------------------------------------------------------


def test_self_method_call_resolves_to_same_class_method(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``self.bar()`` inside ``C.foo`` resolves to ``C.bar``."""
    (tmp_path / "main.py").write_text(
        "class C:\n"
        "    def foo(self):\n"
        "        return self.bar()\n"
        "    def bar(self):\n"
        "        return 1\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    bar_id = _symbol_id(migrated_db, "C.bar")
    rel = next(
        r for r in _relations(migrated_db)
        if r["kind"] == "calls" and r["target_name"] == "self.bar"
    )
    assert rel["target_symbol_id"] == bar_id
    assert rel["confidence"] == "resolved"


def test_self_call_unresolved_when_no_matching_class_method(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``self.nope()`` with no matching method on the enclosing
    class stays unresolved (target_name still recorded).
    """
    (tmp_path / "main.py").write_text(
        "class C:\n"
        "    def foo(self):\n"
        "        return self.nope()\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    rel = next(
        r for r in _relations(migrated_db)
        if r["kind"] == "calls" and r["target_name"] == "self.nope"
    )
    assert rel["target_symbol_id"] is None
    assert rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Always-insert contract
# ---------------------------------------------------------------------------


def test_unresolved_rows_are_inserted_not_dropped(
    migrated_db, py_parser, tmp_path,
) -> None:
    """A call to a function that exists nowhere in the project still
    produces a row — agents querying "what does file X call" must
    see ALL call sites, not just the ones we can resolve.
    """
    (tmp_path / "main.py").write_text(
        "def caller():\n"
        "    return mystery_helper()\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    rels = [
        r for r in _relations(migrated_db)
        if r["target_name"] == "mystery_helper"
    ]
    assert len(rels) == 1
    assert rels[0]["target_symbol_id"] is None
    assert rels[0]["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# UNIQUE discriminator — two callsites at same line different columns
# ---------------------------------------------------------------------------


def test_same_line_two_calls_distinct_rows(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``helper(1); helper(2)`` on the same line — UNIQUE constraint
    (source_symbol_id, target_name, kind, line, column_start)
    discriminates by column_start, so BOTH rows land.
    """
    (tmp_path / "main.py").write_text(
        "def helper(x):\n    return x\n"
        "\n"
        "def caller():\n"
        "    helper(1); helper(2)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    caller_id = _symbol_id(migrated_db, "caller")
    rels = [
        r for r in _relations(migrated_db)
        if r["source_symbol_id"] == caller_id
        and r["target_name"] == "helper"
        and r["kind"] == "calls"
    ]
    assert len(rels) == 2
    columns = sorted(r["column_start"] for r in rels)
    assert columns[0] != columns[1]
    # Both resolve to the same helper symbol — but that's only
    # because the UNIQUE constraint admits both as distinct rows.
    helper_id = _symbol_id(migrated_db, "helper")
    assert all(r["target_symbol_id"] == helper_id for r in rels)


# ---------------------------------------------------------------------------
# Idempotency — second resolve_codebase doesn't double-insert
# ---------------------------------------------------------------------------


def test_resolve_codebase_is_idempotent(
    migrated_db, py_parser, tmp_path,
) -> None:
    """INSERT OR IGNORE against the code_relations UNIQUE constraint
    means a second resolve doesn't duplicate rows.
    """
    (tmp_path / "main.py").write_text(
        "def helper(x): return x\n"
        "def caller(): return helper(1)\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )
    n_before = migrated_db.execute(
        "SELECT COUNT(*) FROM code_relations"
    ).fetchone()[0]
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )
    n_after = migrated_db.execute(
        "SELECT COUNT(*) FROM code_relations"
    ).fetchone()[0]
    assert n_before == n_after
    assert n_before > 0


# ---------------------------------------------------------------------------
# Module-level relations are intentionally skipped
# ---------------------------------------------------------------------------


def test_module_level_relations_are_skipped(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``helper(1)`` at module scope has no source symbol to anchor
    against; the relation is NOT written. Inside a function the same
    call IS written. This documents the v1 trade-off.
    """
    (tmp_path / "main.py").write_text(
        "def helper(x):\n    return x\n"
        "\n"
        "helper(99)\n"  # module-level — skipped
        "\n"
        "def caller():\n"
        "    return helper(1)\n"  # inside function — recorded
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    caller_id = _symbol_id(migrated_db, "caller")
    rels = [
        r for r in _relations(migrated_db)
        if r["target_name"] == "helper" and r["kind"] == "calls"
    ]
    # Only the in-function call is recorded — module-level is dropped.
    assert len(rels) == 1
    assert rels[0]["source_symbol_id"] == caller_id


# ---------------------------------------------------------------------------
# Relative imports are unresolved (v1 limitation)
# ---------------------------------------------------------------------------


def test_relative_import_is_unresolved(
    migrated_db, py_parser, tmp_path,
) -> None:
    """``from .util import helper`` is recorded as an import relation
    but stays unresolved — relative-import resolution would require
    interpreting the source file's package, deferred to a follow-on.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("def helper(): pass\n")
    (pkg / "main.py").write_text(
        "from .util import helper\n"
        "\n"
        "def caller():\n"
        "    return helper()\n"
    )
    _scan_project(migrated_db, tmp_path, py_parser)
    resolve_codebase(
        migrated_db, project_root=tmp_path, parsers={"py": py_parser},
    )

    rels = [
        r for r in _relations(migrated_db) if r["kind"] == "imports"
    ]
    assert all(r["confidence"] == "unresolved" for r in rels)
