"""T5 — Python extraction via real py.scm + sample.py.

Asserts exact symbol set, signatures, parent linkage, kind
disambiguation (FUNCTION vs METHOD), and the canonical edge cases:

- Sibling-shadow nested functions: two ``def outer():`` blocks each
  defining ``def inner_helper()`` produce four rows whose UNIQUE
  discriminator is ``line_start``.
- Same-line duplicate call: ``helper(1); helper(2)`` produces two
  call relations on the same line, differing only in ``column_start``.
- Module-level vs in-function source: ``source_qname`` is empty for
  module-level relations and the innermost enclosing symbol's
  qualified name otherwise.

This module skips wholesale when the ``[codebase]`` extra is not
installed (no tree-sitter pack on the host).
"""

from __future__ import annotations

from pathlib import Path

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")

from sage_memory.codebase._extract import extract


FIXTURE = Path(__file__).parent / "fixtures" / "sample.py"


@pytest.fixture(scope="module")
def extracted():
    parser = tsp.get_parser("python")
    return extract(parser, "py", FIXTURE.read_bytes(), "tests/codebase/fixtures/sample.py")


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


def test_no_error_nodes_on_well_formed_fixture(extracted) -> None:
    assert extracted.had_error_nodes is False


def test_symbol_count(extracted) -> None:
    # helper, Greeter, Greeter.greet, outer x2, outer.inner_helper x2 = 7
    assert len(extracted.symbols) == 7


def test_relation_count(extracted) -> None:
    # 2 imports + 3 calls
    assert len(extracted.relations) == 5


# ---------------------------------------------------------------------------
# Symbols — by line for deterministic comparison
# ---------------------------------------------------------------------------


def _by_line(symbols):
    return {s.line_start: s for s in symbols}


def test_module_function_helper(extracted) -> None:
    s = _by_line(extracted.symbols)[5]
    assert s.name == "helper"
    assert s.qualified_name == "helper"
    assert s.kind == "FUNCTION"
    assert s.line_end == 6
    assert s.parent_qname is None
    assert s.signature == "def helper(x)"


def test_class_greeter(extracted) -> None:
    s = _by_line(extracted.symbols)[8]
    assert s.name == "Greeter"
    assert s.qualified_name == "Greeter"
    assert s.kind == "CLASS"
    assert s.line_end == 10
    assert s.parent_qname is None
    assert s.signature == "class Greeter"


def test_method_greet(extracted) -> None:
    s = _by_line(extracted.symbols)[9]
    assert s.name == "greet"
    assert s.qualified_name == "Greeter.greet"
    assert s.kind == "METHOD"
    assert s.parent_qname == "Greeter"
    assert s.signature == "def greet(self)"


# ---------------------------------------------------------------------------
# Sibling-shadow nested functions — the rev 2 UNIQUE discriminator case
# ---------------------------------------------------------------------------


def test_sibling_shadow_two_outer_symbols(extracted) -> None:
    """Two top-level ``def outer():`` siblings produce two FUNCTION
    rows sharing qname='outer' but with distinct ``line_start`` (12, 16).
    """
    outers = [s for s in extracted.symbols if s.qualified_name == "outer"]
    assert len(outers) == 2
    assert {s.line_start for s in outers} == {12, 16}
    for s in outers:
        assert s.kind == "FUNCTION"
        assert s.parent_qname is None
        assert s.name == "outer"


def test_sibling_shadow_two_inner_helper_symbols(extracted) -> None:
    """Each ``outer`` defines ``inner_helper`` → both qname-collide on
    'outer.inner_helper' but discriminate on ``line_start`` (13, 17).
    """
    inners = [
        s for s in extracted.symbols if s.qualified_name == "outer.inner_helper"
    ]
    assert len(inners) == 2
    assert {s.line_start for s in inners} == {13, 17}
    for s in inners:
        assert s.kind == "FUNCTION"
        assert s.parent_qname == "outer"
        assert s.name == "inner_helper"


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def test_imports(extracted) -> None:
    imports = [r for r in extracted.relations if r.kind == "imports"]
    targets = [r.target_name for r in imports]
    assert "os" in targets
    assert "collections.deque" in targets
    # Module-level imports — source is empty.
    for r in imports:
        assert r.source_qname == ""


def test_import_lines(extracted) -> None:
    imports = {r.target_name: r for r in extracted.relations if r.kind == "imports"}
    assert imports["os"].line == 2
    assert imports["collections.deque"].line == 3


def test_call_in_method_attributed_to_method_source(extracted) -> None:
    """`return helper(self)` on line 10 — source_qname is the
    enclosing method's qname, not the file's.
    """
    method_calls = [
        r for r in extracted.relations
        if r.kind == "calls" and r.line == 10
    ]
    assert len(method_calls) == 1
    assert method_calls[0].target_name == "helper"
    assert method_calls[0].source_qname == "Greeter.greet"


def test_same_line_duplicate_call(extracted) -> None:
    """`helper(1); helper(2)` on line 20 produces two relations
    differing only in ``column_start`` — the rev 2 same-line duplicate
    UNIQUE discriminator case (code_relations UNIQUE includes
    column_start).
    """
    line_20 = [
        r for r in extracted.relations
        if r.kind == "calls" and r.line == 20
    ]
    assert len(line_20) == 2
    columns = sorted(r.column_start for r in line_20)
    assert columns[0] != columns[1]  # distinct columns
    for r in line_20:
        assert r.target_name == "helper"
        assert r.source_qname == ""  # module-level


# ---------------------------------------------------------------------------
# Determinism — second extract() call returns equivalent payload
# ---------------------------------------------------------------------------


def test_extract_is_deterministic() -> None:
    parser = tsp.get_parser("python")
    bytes_ = FIXTURE.read_bytes()
    a = extract(parser, "py", bytes_, "x.py")
    b = extract(parser, "py", bytes_, "x.py")
    assert [
        (s.qualified_name, s.kind, s.line_start) for s in a.symbols
    ] == [
        (s.qualified_name, s.kind, s.line_start) for s in b.symbols
    ]
    assert [
        (r.target_name, r.kind, r.line, r.column_start) for r in a.relations
    ] == [
        (r.target_name, r.kind, r.line, r.column_start) for r in b.relations
    ]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_file_returns_empty_payload(tmp_path: Path) -> None:
    parser = tsp.get_parser("python")
    result = extract(parser, "py", b"", "empty.py")
    assert result.symbols == []
    assert result.relations == []
    assert result.had_error_nodes is False


def test_syntactically_broken_file_recovers_with_error_node(tmp_path: Path) -> None:
    """Tree-sitter is error-recovering: extract still succeeds, but
    ``had_error_nodes`` flips True so callers can flag the file.
    """
    parser = tsp.get_parser("python")
    src = b"def f(:\n    return 1\n"  # missing param
    result = extract(parser, "py", src, "broken.py")
    assert result.had_error_nodes is True


def test_unimplemented_query_id_raises() -> None:
    """Use a deliberately fake query_id — T8 adds rb/php/c/cpp; this
    test should not regress as new languages come online.
    """
    parser = tsp.get_parser("python")
    with pytest.raises(NotImplementedError):
        extract(parser, "unknown_query_id_xyz", b"", "x.xyz")
