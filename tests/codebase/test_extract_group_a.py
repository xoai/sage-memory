"""T6 — TypeScript / TSX / JSX / JavaScript extraction.

Two layers:

1. Walker × extractor E2E: confirm each fixture's extension routes
   to the right (grammar, query_id) tuple per spec rev 3 plan line 86,
   and that extraction with that combo produces the expected symbol
   shape.

2. Per-fixture assertions covering — named arrow function captured
   as FUNCTION, JSX function-components captured as FUNCTION
   (not CLASS), member-expression calls dotted correctly, named
   imports keyed on the original (non-alias) identifier, namespace
   imports collapse to the module path, default imports name the
   ``.default`` slot, sibling-shadow nested functions on the SAME
   line, same-line duplicate calls.

Module-level ``importorskip`` so the file is fully skipped when the
[codebase] extra isn't installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase._extract import extract
from sage_memory.codebase._walker import walk


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Walker × extractor combo for each extension
# ---------------------------------------------------------------------------


def _walker_tuple_for(name: str) -> tuple[str, str, str, str]:
    """Walk the fixtures dir and find the tuple for the given filename."""
    results = list(walk(FIXTURES_DIR, include_ignored=True))
    for rel, lang, grammar, qid in results:
        if rel.name == name:
            return (str(rel), lang, grammar, qid)
    raise AssertionError(f"Walker did not yield {name}")


@pytest.mark.parametrize(
    "filename,language,grammar,query_id",
    [
        ("sample.ts",  "ts", "typescript", "ts"),
        ("sample.tsx", "ts", "tsx",        "tsx"),
        ("sample.jsx", "js", "tsx",        "js"),
        ("sample.js",  "js", "javascript", "js"),
    ],
)
def test_walker_routes_extension_to_grammar_and_query(
    filename: str, language: str, grammar: str, query_id: str,
) -> None:
    _rel, walked_lang, walked_grammar, walked_qid = _walker_tuple_for(filename)
    assert walked_lang == language
    assert walked_grammar == grammar
    assert walked_qid == query_id


@pytest.mark.parametrize(
    "filename,grammar,query_id",
    [
        ("sample.ts",  "typescript", "ts"),
        ("sample.tsx", "tsx",        "tsx"),
        ("sample.jsx", "tsx",        "js"),
        ("sample.js",  "javascript", "js"),
    ],
)
def test_extract_succeeds_for_each_combo(
    filename: str, grammar: str, query_id: str,
) -> None:
    parser = tsp.get_parser(grammar)
    src = (FIXTURES_DIR / filename).read_bytes()
    result = extract(parser, query_id, src, filename)
    # Tree-sitter is error-recovering; for well-formed fixtures the
    # tree must be clean.
    assert result.had_error_nodes is False
    assert len(result.symbols) > 0
    assert len(result.relations) > 0


# ---------------------------------------------------------------------------
# sample.ts — full TS surface (function, class, method, interface, arrow,
# sibling-shadow, same-line dup call, named + namespace imports)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ts_extracted():
    parser = tsp.get_parser("typescript")
    return extract(
        parser, "ts",
        (FIXTURES_DIR / "sample.ts").read_bytes(),
        "sample.ts",
    )


def test_ts_all_symbol_kinds_covered(ts_extracted) -> None:
    """helper, Greeter, Greeter.greet, Animal, arrow, 2× outer,
    2× outer.inner_helper = 9 total.
    """
    qnames = sorted(s.qualified_name for s in ts_extracted.symbols)
    assert qnames == sorted([
        "helper",
        "Greeter", "Greeter.greet",
        "Animal",
        "arrow",
        "outer", "outer",
        "outer.inner_helper", "outer.inner_helper",
    ])


def test_ts_interface_kind(ts_extracted) -> None:
    animal = next(s for s in ts_extracted.symbols if s.qualified_name == "Animal")
    assert animal.kind == "INTERFACE"


def test_ts_arrow_function_kind_is_function(ts_extracted) -> None:
    """Named arrow function `const arrow = (x) => ...` becomes a
    FUNCTION symbol with the variable's name. Anonymous arrows are NOT
    captured as their own symbols (they don't pollute the symbol set).
    """
    arrow = next(s for s in ts_extracted.symbols if s.qualified_name == "arrow")
    assert arrow.kind == "FUNCTION"


def test_ts_method_kind(ts_extracted) -> None:
    greet = next(
        s for s in ts_extracted.symbols if s.qualified_name == "Greeter.greet"
    )
    assert greet.kind == "METHOD"
    assert greet.parent_qname == "Greeter"


def test_ts_sibling_shadow_two_outers_same_line(ts_extracted) -> None:
    """Both ``function outer() { ... }`` siblings are on consecutive
    single-line defs (lines 13, 14). UNIQUE discriminator on
    (qname, kind, line_start) — different line_starts.
    """
    outers = [s for s in ts_extracted.symbols if s.qualified_name == "outer"]
    assert len(outers) == 2
    assert sorted(s.line_start for s in outers) == [13, 14]


def test_ts_named_import_targets_with_alias_dropped(ts_extracted) -> None:
    """`import { foo } from "./bar"` → target_name = "./bar.foo" using
    the original name (never the alias).
    """
    imports = [
        r for r in ts_extracted.relations
        if r.kind == "imports" and r.target_name == "./bar.foo"
    ]
    assert len(imports) == 1


def test_ts_namespace_import_collapses_to_module(ts_extracted) -> None:
    """`import * as ns from "lib"` → target_name = "lib"."""
    ns_imports = [
        r for r in ts_extracted.relations
        if r.kind == "imports" and r.target_name == "lib"
    ]
    assert len(ns_imports) == 1


def test_ts_same_line_duplicate_call(ts_extracted) -> None:
    """`helper(1); helper(2)` on the same line → two relations differing
    only in column_start.
    """
    calls = [
        r for r in ts_extracted.relations
        if r.kind == "calls" and r.target_name == "helper"
    ]
    last_line_calls = [r for r in calls if r.line == max(c.line for c in calls)]
    assert len(last_line_calls) == 2
    assert last_line_calls[0].column_start != last_line_calls[1].column_start


def test_ts_call_inside_method_source_qname(ts_extracted) -> None:
    """The `helper(1)` call inside `Greeter.greet` body has
    source_qname='Greeter.greet'.
    """
    greet_calls = [
        r for r in ts_extracted.relations
        if r.kind == "calls" and r.source_qname == "Greeter.greet"
    ]
    assert len(greet_calls) >= 1
    assert all(r.target_name == "helper" for r in greet_calls)


def test_ts_call_inside_arrow_source_qname(ts_extracted) -> None:
    """The `helper(x)` call inside `const arrow = ... => helper(x)`
    has source_qname='arrow'.
    """
    arrow_calls = [
        r for r in ts_extracted.relations
        if r.kind == "calls" and r.source_qname == "arrow"
    ]
    assert len(arrow_calls) == 1
    assert arrow_calls[0].target_name == "helper"


# ---------------------------------------------------------------------------
# sample.tsx — JSX-returning function-component as FUNCTION
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tsx_extracted():
    parser = tsp.get_parser("tsx")
    return extract(
        parser, "tsx",
        (FIXTURES_DIR / "sample.tsx").read_bytes(),
        "sample.tsx",
    )


def test_tsx_function_component_is_function_not_class(tsx_extracted) -> None:
    """`function Greeter(props) { return <div>...</div>; }` is a
    function declaration, NOT a class — even though it has a
    capital-letter name and renders JSX. We capture it as FUNCTION.
    """
    greeter = next(
        s for s in tsx_extracted.symbols if s.qualified_name == "Greeter"
    )
    assert greeter.kind == "FUNCTION"


def test_tsx_default_import(tsx_extracted) -> None:
    """`import React from "react"` → target_name='react.default'."""
    react = [
        r for r in tsx_extracted.relations
        if r.kind == "imports" and r.target_name == "react.default"
    ]
    assert len(react) == 1


def test_tsx_call_inside_jsx_expression_container_resolved(tsx_extracted) -> None:
    """The `{helper(1)}` call inside `<div>{...}</div>` must be
    captured — JSX expression containers expose the call expression
    to the tree-sitter call query.
    """
    calls = [
        r for r in tsx_extracted.relations
        if r.kind == "calls" and r.source_qname == "Greeter"
    ]
    assert len(calls) >= 1


# ---------------------------------------------------------------------------
# sample.jsx — uses tsx grammar + js query
# ---------------------------------------------------------------------------


def test_jsx_extract_does_not_match_interface_or_enum_queries() -> None:
    """The js query (used for .jsx) intentionally omits
    interface_declaration / enum_declaration — those would fail to
    compile against the javascript grammar but compile fine against
    the tsx grammar. Either way the jsx fixture contains neither, so
    the symbol set must contain only the four real entries.
    """
    parser = tsp.get_parser("tsx")
    result = extract(
        parser, "js",
        (FIXTURES_DIR / "sample.jsx").read_bytes(),
        "sample.jsx",
    )
    qnames = sorted(s.qualified_name for s in result.symbols)
    assert qnames == ["Greeter", "arrow", "helper"]


# ---------------------------------------------------------------------------
# sample.js — JS without TS types, sibling-shadow + dup-call repeat
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def js_extracted():
    parser = tsp.get_parser("javascript")
    return extract(
        parser, "js",
        (FIXTURES_DIR / "sample.js").read_bytes(),
        "sample.js",
    )


def test_js_class_and_method_kinds(js_extracted) -> None:
    g = next(s for s in js_extracted.symbols if s.qualified_name == "Greeter")
    assert g.kind == "CLASS"
    greet = next(
        s for s in js_extracted.symbols if s.qualified_name == "Greeter.greet"
    )
    assert greet.kind == "METHOD"


def test_js_sibling_shadow(js_extracted) -> None:
    outers = [s for s in js_extracted.symbols if s.qualified_name == "outer"]
    assert len(outers) == 2
    assert sorted(s.line_start for s in outers) == [12, 13]


def test_js_same_line_duplicate_call(js_extracted) -> None:
    calls = [
        r for r in js_extracted.relations
        if r.kind == "calls" and r.target_name == "helper"
    ]
    last_line = max(c.line for c in calls)
    last_line_calls = [r for r in calls if r.line == last_line]
    assert len(last_line_calls) == 2
    assert last_line_calls[0].column_start != last_line_calls[1].column_start


def test_js_no_interface_symbols(js_extracted) -> None:
    """Plain JS has no interface keyword; the js query must not surface
    INTERFACE symbols even if the parser happens to encounter
    identifiers named ``interface`` (it wouldn't here, but proves the
    contract).
    """
    assert not any(s.kind == "INTERFACE" for s in js_extracted.symbols)


# ---------------------------------------------------------------------------
# Cross-fixture: parent_id linkage E2E inside the extractor
# ---------------------------------------------------------------------------


def test_ts_inner_helpers_distinguish_via_parent_id(ts_extracted) -> None:
    """Two ``outer.inner_helper`` symbols must point at DIFFERENT
    parent_ids (one for each ``outer`` sibling) so the DB FK preserves
    the source structure.
    """
    inners = [
        s for s in ts_extracted.symbols
        if s.qualified_name == "outer.inner_helper"
    ]
    assert len(inners) == 2
    assert inners[0].parent_id != inners[1].parent_id
    # And each parent_id should be one of the two outer symbols' ids.
    outer_ids = {
        s.id for s in ts_extracted.symbols if s.qualified_name == "outer"
    }
    assert inners[0].parent_id in outer_ids
    assert inners[1].parent_id in outer_ids
