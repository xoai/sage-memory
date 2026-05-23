"""T9b — 8 per-language resolvers (1 positive + 1 negative each).

Mirrors the explicit test matrix from plan rev 3 line 91:

| Lang  | Positive                                           | Negative                                        |
|-------|----------------------------------------------------|-------------------------------------------------|
| TS    | ``import { x } from "./foo"`` + ``./foo.ts``       | ``import { x } from "@scope/pkg"``              |
| JS    | ``import { x } from "./foo"`` + ``./foo.js``       | ``import { x } from "@scope/pkg"``              |
| Go    | Same-package call (sibling file in same dir)       | ``import "github.com/external/lib"``            |
| Rust  | ``mod foo;`` + ``foo::bar()`` + sibling ``foo.rs`` | ``use external::crate::Foo``                    |
| Java  | Same-package class call (sibling file in same dir) | ``import com.external.Lib``                     |
| Ruby  | Same-file ``def foo`` + ``foo()``                  | Cross-file ``foo()`` with no shared require     |
| PHP   | Same-file function call                            | Cross-file (no autoload tracking)               |
| C     | Same-file ``int helper()`` + call                  | ``#include <ext.h>``                            |
| C++   | Same-file class method call                        | ``#include <ext.hpp>``                          |

Each test creates a tiny project layout in tmp_path, scans every
matching source file via T5's ``_scan_file``, runs ``resolve_codebase``,
and inspects the ``code_relations`` rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase import _scan_file
from sage_memory.codebase._languages import EXT_MAP, resolve_h_file
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


def _scan_all(conn, project_root: Path, parsers: dict[str, object]) -> None:
    """Walk project_root, scanning every file whose extension is in
    EXT_MAP (or a .h, resolved via the heuristic).
    """
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext in EXT_MAP:
            language_tag, grammar_name, query_id = EXT_MAP[ext]
        elif ext == ".h":
            language_tag, grammar_name, query_id = resolve_h_file(path.parent)
        else:
            continue
        parser = parsers.get(language_tag) or parsers.get(grammar_name)
        if parser is None:
            continue
        rel = str(path.relative_to(project_root))
        _scan_file(
            conn,
            abs_path=path,
            rel_path=rel,
            language_tag=language_tag,
            query_id=query_id,
            parser=parser,
            force=False,
        )


def _resolve_and_relations(
    conn, project_root: Path, parsers: dict[str, object],
) -> list[dict]:
    resolve_codebase(conn, project_root=project_root, parsers=parsers)
    return [
        dict(r) for r in conn.execute(
            "SELECT source_symbol_id, target_symbol_id, target_name, "
            "kind, confidence FROM code_relations"
        )
    ]


# ---------------------------------------------------------------------------
# TypeScript
# ---------------------------------------------------------------------------


def test_ts_pos_local_import_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "b.ts").write_text("export function foo() { return 1; }\n")
    (tmp_path / "a.ts").write_text(
        'import { foo } from "./b";\n'
        "function caller() { return foo(); }\n"
    )
    parsers = {"ts": tsp.get_parser("typescript"), "typescript": tsp.get_parser("typescript")}
    _scan_all(migrated_db, tmp_path, parsers)

    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)
    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "./b.foo"
    )
    assert import_rel["confidence"] == "resolved"
    assert import_rel["target_symbol_id"] is not None


def test_ts_neg_external_scoped_import_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "a.ts").write_text(
        'import { x } from "@scope/pkg";\n'
        "function caller() { return x(); }\n"
    )
    parsers = {"ts": tsp.get_parser("typescript")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "@scope/pkg.x"
    )
    assert import_rel["confidence"] == "unresolved"
    assert import_rel["target_symbol_id"] is None


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------


def test_js_pos_local_import_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "b.js").write_text("export function foo() { return 1; }\n")
    (tmp_path / "a.js").write_text(
        'import { foo } from "./b";\n'
        "function caller() { return foo(); }\n"
    )
    parsers = {"js": tsp.get_parser("javascript"), "javascript": tsp.get_parser("javascript")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "./b.foo"
    )
    assert import_rel["confidence"] == "resolved"


def test_js_neg_external_import_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "a.js").write_text(
        'import { x } from "@scope/pkg";\n'
        "function caller() { return x(); }\n"
    )
    parsers = {"js": tsp.get_parser("javascript")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "@scope/pkg.x"
    )
    assert import_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def test_go_pos_same_package_call_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "b.go").write_text(
        "package main\n\n"
        "func helper() int { return 1 }\n"
    )
    (tmp_path / "a.go").write_text(
        "package main\n\n"
        "func caller() int { return helper() }\n"
    )
    parsers = {"go": tsp.get_parser("go")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "helper"
    )
    assert call_rel["confidence"] == "resolved"
    assert call_rel["target_symbol_id"] is not None


def test_go_neg_external_import_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "a.go").write_text(
        'package main\n'
        '\n'
        'import "github.com/external/lib"\n'
        '\n'
        'func caller() { lib.Foo() }\n'
    )
    parsers = {"go": tsp.get_parser("go")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "github.com/external/lib"
    )
    assert import_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


def test_rs_pos_mod_sibling_resolves(migrated_db, tmp_path) -> None:
    """``mod foo;`` declared in ``a.rs`` + sibling ``foo.rs`` with
    ``fn bar``. ``foo::bar()`` call in a.rs resolves to bar's symbol.

    The resolver does NOT actually look at the ``mod foo;`` declaration
    — it uses the same-directory file-naming convention. ``mod foo;``
    is what would make ``foo`` reachable in real Rust; we trust the
    fixture's well-formedness.
    """
    (tmp_path / "foo.rs").write_text(
        "pub fn bar() -> i32 { 1 }\n"
    )
    (tmp_path / "a.rs").write_text(
        "mod foo;\n"
        "\n"
        "fn caller() -> i32 { foo::bar() }\n"
    )
    parsers = {"rs": tsp.get_parser("rust")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "foo::bar"
    )
    assert call_rel["confidence"] == "resolved"


def test_rs_pos_same_file_scoped_call_resolves(migrated_db, tmp_path) -> None:
    """T11 Major #4 regression: ``Point::new()`` *in the same file*
    that defines ``Point`` must resolve. The extractor stores the
    target_name with ``::`` but symbols' qualified_name uses ``.``;
    the resolver normalizes before the same-file lookup.
    """
    (tmp_path / "a.rs").write_text(
        "pub struct Point;\n"
        "\n"
        "impl Point {\n"
        "    pub fn new() -> Self { Point }\n"
        "}\n"
        "\n"
        "fn caller() -> Point { Point::new() }\n"
    )
    parsers = {"rs": tsp.get_parser("rust")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "Point::new"
    )
    assert call_rel["confidence"] == "resolved"
    assert call_rel["target_symbol_id"] is not None


def test_rs_neg_external_use_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "a.rs").write_text(
        "use external::crate_::Foo;\n"
        "\n"
        "fn caller() -> i32 { 1 }\n"
    )
    parsers = {"rs": tsp.get_parser("rust")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports"
        and r["target_name"] == "external::crate_::Foo"
    )
    assert import_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


def test_java_pos_same_package_class_call_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "B.java").write_text(
        "package x;\n"
        "class B { public static int helper() { return 1; } }\n"
    )
    (tmp_path / "A.java").write_text(
        "package x;\n"
        "class A { void caller() { B.helper(); } }\n"
    )
    parsers = {"java": tsp.get_parser("java")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "B.helper"
    )
    assert call_rel["confidence"] == "resolved"


def test_java_neg_external_import_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "A.java").write_text(
        "package x;\n"
        "import com.external.Lib;\n"
        "class A { void caller() { Lib.foo(); } }\n"
    )
    parsers = {"java": tsp.get_parser("java")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "com.external.Lib"
    )
    assert import_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# Ruby
# ---------------------------------------------------------------------------


def test_rb_pos_same_file_resolves(migrated_db, tmp_path) -> None:
    # Plan rev 3 line 91 explicitly specifies ``foo()`` with parens —
    # bare ``foo`` is parsed as an ``identifier`` (ambiguous local
    # var vs method call) and not captured by the call query.
    (tmp_path / "main.rb").write_text(
        "def foo\n  1\nend\n\n"
        "def caller\n  foo()\nend\n"
    )
    parsers = {"rb": tsp.get_parser("ruby")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "foo"
    )
    assert call_rel["confidence"] == "resolved"


def test_rb_neg_cross_file_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "lib.rb").write_text("def foo\n  1\nend\n")
    (tmp_path / "main.rb").write_text(
        "def caller\n  foo()\nend\n"
    )
    parsers = {"rb": tsp.get_parser("ruby")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rels = [
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "foo"
    ]
    # ``caller`` calls ``foo`` but ``foo`` is defined in a different
    # file with no ``require`` connecting them — Ruby resolver does
    # not cross files in v1.
    caller_call = next(
        r for r in call_rels
        if r["source_symbol_id"] is not None  # in-function call only
    )
    assert caller_call["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------


def test_php_pos_same_file_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "main.php").write_text(
        "<?php\n"
        "function helper(): int { return 1; }\n"
        "function caller(): int { return helper(); }\n"
    )
    parsers = {"php": tsp.get_parser("php")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "helper"
    )
    assert call_rel["confidence"] == "resolved"


def test_php_neg_cross_file_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "lib.php").write_text(
        "<?php\nfunction helper(): int { return 1; }\n"
    )
    (tmp_path / "main.php").write_text(
        "<?php\nfunction caller(): int { return helper(); }\n"
    )
    parsers = {"php": tsp.get_parser("php")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls"
        and r["target_name"] == "helper"
        and r["source_symbol_id"] is not None
    )
    assert call_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# C
# ---------------------------------------------------------------------------


def test_c_pos_same_file_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "main.c").write_text(
        "int helper(int x) { return x + 1; }\n"
        "int caller(void) { return helper(1); }\n"
    )
    parsers = {"c": tsp.get_parser("c")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "helper"
    )
    assert call_rel["confidence"] == "resolved"


def test_c_neg_system_include_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "main.c").write_text(
        "#include <stdio.h>\n"
        "int caller(void) { return 0; }\n"
    )
    parsers = {"c": tsp.get_parser("c")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "stdio.h"
    )
    assert import_rel["confidence"] == "unresolved"


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------


def test_cpp_pos_same_file_resolves(migrated_db, tmp_path) -> None:
    (tmp_path / "main.cpp").write_text(
        "int helper(int x) { return x + 1; }\n"
        "int caller() { return helper(1); }\n"
    )
    parsers = {"cpp": tsp.get_parser("cpp")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    call_rel = next(
        r for r in rels
        if r["kind"] == "calls" and r["target_name"] == "helper"
    )
    assert call_rel["confidence"] == "resolved"


def test_cpp_neg_system_include_unresolved(migrated_db, tmp_path) -> None:
    (tmp_path / "main.cpp").write_text(
        "#include <ext.hpp>\n"
        "int caller() { return 0; }\n"
    )
    parsers = {"cpp": tsp.get_parser("cpp")}
    _scan_all(migrated_db, tmp_path, parsers)
    rels = _resolve_and_relations(migrated_db, tmp_path, parsers)

    import_rel = next(
        r for r in rels
        if r["kind"] == "imports" and r["target_name"] == "ext.hpp"
    )
    assert import_rel["confidence"] == "unresolved"
