"""T7 — Go / Rust / Java extraction (Query Group B).

Each language section covers — symbol kinds (FUNCTION, METHOD,
CLASS, STRUCT, INTERFACE, ENUM as applicable), the
``ImplType.MethodName`` qualified_name pattern, method parent_id
linkage to the type symbol declared in the same file, import target
shapes, and call relation source_qname attribution.

Module-level ``importorskip`` so the whole file is skipped when the
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
    for rel, lang, grammar, qid in walk(FIXTURES_DIR, include_ignored=True):
        if rel.name == name:
            return (str(rel), lang, grammar, qid)
    raise AssertionError(f"Walker did not yield {name}")


@pytest.mark.parametrize(
    "filename,language,grammar,query_id",
    [
        ("sample.go",   "go",   "go",   "go"),
        ("sample.rs",   "rs",   "rust", "rs"),
        ("sample.java", "java", "java", "java"),
    ],
)
def test_walker_routes_extension_to_grammar_and_query(
    filename: str, language: str, grammar: str, query_id: str,
) -> None:
    _, walked_lang, walked_grammar, walked_qid = _walker_tuple_for(filename)
    assert walked_lang == language
    assert walked_grammar == grammar
    assert walked_qid == query_id


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def go_extracted():
    parser = tsp.get_parser("go")
    return extract(
        parser, "go",
        (FIXTURES_DIR / "sample.go").read_bytes(),
        "sample.go",
    )


def test_go_no_error_nodes(go_extracted) -> None:
    assert go_extracted.had_error_nodes is False


def test_go_symbol_set(go_extracted) -> None:
    qnames = sorted(s.qualified_name for s in go_extracted.symbols)
    assert qnames == sorted(["Person", "Person.Greet", "helper", "main"])


def test_go_struct_kind(go_extracted) -> None:
    p = next(s for s in go_extracted.symbols if s.qualified_name == "Person")
    assert p.kind == "STRUCT"


def test_go_method_with_pointer_receiver_unwrapped(go_extracted) -> None:
    """``func (p *Person) Greet()`` → METHOD with qname=``Person.Greet``
    (the pointer is unwrapped to the receiver type).
    """
    greet = next(
        s for s in go_extracted.symbols if s.qualified_name == "Person.Greet"
    )
    assert greet.kind == "METHOD"
    assert greet.parent_qname == "Person"


def test_go_method_parent_id_links_to_struct(go_extracted) -> None:
    """The METHOD's parent_id points at the STRUCT row declared in
    the same file — the FK linkage closes locally without needing
    cross-file resolution.
    """
    person = next(s for s in go_extracted.symbols if s.qualified_name == "Person")
    greet = next(
        s for s in go_extracted.symbols if s.qualified_name == "Person.Greet"
    )
    assert greet.parent_id == person.id


def test_go_imports_unquote_to_module_path(go_extracted) -> None:
    """Go ``import "fmt"`` → target_name="fmt" (quotes stripped)."""
    imports = sorted(
        r.target_name for r in go_extracted.relations if r.kind == "imports"
    )
    assert imports == ["fmt", "os"]


def test_go_call_inside_method_source_qname(go_extracted) -> None:
    """The ``helper(p.Name)`` call inside ``Person.Greet`` body has
    source_qname='Person.Greet'.
    """
    call = next(
        r for r in go_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "Person.Greet"
    )
    assert call is not None


def test_go_selector_expression_dotted(go_extracted) -> None:
    """``fmt.Sprintln(...)`` → target_name='fmt.Sprintln'."""
    fmt_call = [
        r for r in go_extracted.relations
        if r.kind == "calls" and r.target_name == "fmt.Sprintln"
    ]
    assert len(fmt_call) == 1


def test_go_same_line_duplicate_call(go_extracted) -> None:
    """``helper("a"); helper("b")`` on the same line → two distinct
    call relations.
    """
    inside_main = [
        r for r in go_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "main"
    ]
    last_line = max(r.line for r in inside_main)
    same_line = [r for r in inside_main if r.line == last_line]
    assert len(same_line) == 2
    assert same_line[0].column_start != same_line[1].column_start


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rs_extracted():
    parser = tsp.get_parser("rust")
    return extract(
        parser, "rs",
        (FIXTURES_DIR / "sample.rs").read_bytes(),
        "sample.rs",
    )


def test_rs_no_error_nodes(rs_extracted) -> None:
    assert rs_extracted.had_error_nodes is False


def test_rs_symbol_set(rs_extracted) -> None:
    qnames = sorted(s.qualified_name for s in rs_extracted.symbols)
    assert qnames == sorted([
        "Point", "Point.new", "Point.distance",
        "Color", "helper", "main",
    ])


def test_rs_struct_kind(rs_extracted) -> None:
    point = next(s for s in rs_extracted.symbols if s.qualified_name == "Point")
    assert point.kind == "STRUCT"


def test_rs_enum_kind(rs_extracted) -> None:
    color = next(s for s in rs_extracted.symbols if s.qualified_name == "Color")
    assert color.kind == "ENUM"


def test_rs_fn_inside_impl_is_method(rs_extracted) -> None:
    """``impl Point { fn new(...) {} }`` → METHOD with qname=``Point.new``.
    Tests the impl_item parent walk.
    """
    new = next(
        s for s in rs_extracted.symbols if s.qualified_name == "Point.new"
    )
    assert new.kind == "METHOD"
    assert new.parent_qname == "Point"


def test_rs_method_parent_id_links_to_struct(rs_extracted) -> None:
    point = next(s for s in rs_extracted.symbols if s.qualified_name == "Point")
    distance = next(
        s for s in rs_extracted.symbols if s.qualified_name == "Point.distance"
    )
    assert distance.parent_id == point.id


def test_rs_top_level_fn_kind(rs_extracted) -> None:
    helper = next(s for s in rs_extracted.symbols if s.qualified_name == "helper")
    assert helper.kind == "FUNCTION"
    assert helper.parent_qname is None


def test_rs_use_imports_scoped_identifier(rs_extracted) -> None:
    """``use std::collections::HashMap;`` → target_name with the
    ``::`` separator preserved.
    """
    uses = [r for r in rs_extracted.relations if r.kind == "imports"]
    assert len(uses) == 1
    assert uses[0].target_name == "std::collections::HashMap"


def test_rs_scoped_call_keeps_double_colon(rs_extracted) -> None:
    """``Point::new(1, 2)`` → target_name='Point::new'."""
    pn = [
        r for r in rs_extracted.relations
        if r.kind == "calls" and r.target_name == "Point::new"
    ]
    assert len(pn) == 1


def test_rs_field_call_dotted(rs_extracted) -> None:
    """``p.distance()`` → target_name='p.distance' (field_expression
    walked through to a dotted name).
    """
    pd = [
        r for r in rs_extracted.relations
        if r.kind == "calls" and r.target_name == "p.distance"
    ]
    assert len(pd) == 1


def test_rs_same_line_duplicate_call(rs_extracted) -> None:
    inside_main = [
        r for r in rs_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "main"
    ]
    assert len(inside_main) == 2
    assert inside_main[0].column_start != inside_main[1].column_start


# ---------------------------------------------------------------------------
# Java
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def java_extracted():
    parser = tsp.get_parser("java")
    return extract(
        parser, "java",
        (FIXTURES_DIR / "sample.java").read_bytes(),
        "sample.java",
    )


def test_java_no_error_nodes(java_extracted) -> None:
    assert java_extracted.had_error_nodes is False


def test_java_symbol_set(java_extracted) -> None:
    """8 symbols: 2 classes, 1 interface, 1 enum, 4 methods (greet,
    helper, name (interface method), main).
    """
    qnames = sorted(s.qualified_name for s in java_extracted.symbols)
    assert qnames == sorted([
        "Greeter", "Greeter.greet", "Greeter.helper",
        "Animal", "Animal.name",
        "Color",
        "Main", "Main.main",
    ])


def test_java_class_kind(java_extracted) -> None:
    g = next(s for s in java_extracted.symbols if s.qualified_name == "Greeter")
    assert g.kind == "CLASS"


def test_java_interface_kind(java_extracted) -> None:
    a = next(s for s in java_extracted.symbols if s.qualified_name == "Animal")
    assert a.kind == "INTERFACE"


def test_java_enum_kind(java_extracted) -> None:
    c = next(s for s in java_extracted.symbols if s.qualified_name == "Color")
    assert c.kind == "ENUM"


def test_java_method_kind_and_parent_qname(java_extracted) -> None:
    greet = next(
        s for s in java_extracted.symbols if s.qualified_name == "Greeter.greet"
    )
    assert greet.kind == "METHOD"
    assert greet.parent_qname == "Greeter"


def test_java_interface_method_is_method(java_extracted) -> None:
    """Methods inside ``interface`` bodies (``String name();``) are
    still METHOD with parent=interface — uniform handling across
    class / interface / enum bodies.
    """
    n = next(
        s for s in java_extracted.symbols if s.qualified_name == "Animal.name"
    )
    assert n.kind == "METHOD"
    assert n.parent_qname == "Animal"


def test_java_method_parent_id_links_to_class(java_extracted) -> None:
    greeter = next(
        s for s in java_extracted.symbols if s.qualified_name == "Greeter"
    )
    greet = next(
        s for s in java_extracted.symbols if s.qualified_name == "Greeter.greet"
    )
    assert greet.parent_id == greeter.id


def test_java_imports_are_scoped_identifiers(java_extracted) -> None:
    imports = sorted(
        r.target_name for r in java_extracted.relations if r.kind == "imports"
    )
    assert imports == ["java.util.List", "java.util.Map"]


def test_java_method_invocation_with_object_dotted(java_extracted) -> None:
    """``g.greet("world")`` produces target_name='g.greet' — the
    object receiver and method name joined.
    """
    gg = [
        r for r in java_extracted.relations
        if r.kind == "calls" and r.target_name == "g.greet"
    ]
    assert len(gg) == 1


def test_java_call_inside_method_source_qname(java_extracted) -> None:
    """``helper(1)`` inside Greeter.greet body has
    source_qname='Greeter.greet'.
    """
    call = [
        r for r in java_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "Greeter.greet"
    ]
    assert len(call) == 1


def test_java_call_inside_main_source_qname(java_extracted) -> None:
    """``g.greet("world")`` inside Main.main has
    source_qname='Main.main'.
    """
    call = [
        r for r in java_extracted.relations
        if r.kind == "calls"
        and r.target_name == "g.greet"
        and r.source_qname == "Main.main"
    ]
    assert len(call) == 1
