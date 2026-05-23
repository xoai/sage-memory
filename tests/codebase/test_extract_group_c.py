"""T8 — Ruby / PHP / C / C++ extraction (Query Group C).

Plus the canonical **`.h` cohabitation E2E**: the walker resolves
``sample.h`` to ``cpp`` when ``sample.cpp`` lives in the same
directory, then the extractor (driven by query_id=cpp) actually parses
the header and emits its symbols.

Each language section covers — symbol kinds (FUNCTION, METHOD, CLASS,
STRUCT, INTERFACE as applicable), method parent_id linkage to the
class/struct declared in the same file, import target shapes
(``require`` / ``use`` / ``#include``), and call relation source_qname
attribution.

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
# Ruby
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rb_extracted():
    parser = tsp.get_parser("ruby")
    return extract(
        parser, "rb",
        (FIXTURES_DIR / "sample.rb").read_bytes(),
        "sample.rb",
    )


def test_rb_no_error_nodes(rb_extracted) -> None:
    assert rb_extracted.had_error_nodes is False


def test_rb_symbol_set(rb_extracted) -> None:
    """The ``module Foo`` is intentionally NOT a symbol — Ruby's
    ``module`` doesn't map to any kind in the spec's enum, so we
    emit only the class + class method + top-level def. The class's
    parent_qname is None even though it lives inside ``module Foo``.
    """
    qnames = sorted(s.qualified_name for s in rb_extracted.symbols)
    assert qnames == sorted(["Bar", "Bar.greet", "helper"])


def test_rb_class_kind(rb_extracted) -> None:
    bar = next(s for s in rb_extracted.symbols if s.qualified_name == "Bar")
    assert bar.kind == "CLASS"
    assert bar.parent_qname is None


def test_rb_method_kind(rb_extracted) -> None:
    greet = next(s for s in rb_extracted.symbols if s.qualified_name == "Bar.greet")
    assert greet.kind == "METHOD"
    assert greet.parent_qname == "Bar"


def test_rb_top_level_def_is_function(rb_extracted) -> None:
    """A ``def`` at file scope (not inside a class) is FUNCTION."""
    h = next(s for s in rb_extracted.symbols if s.qualified_name == "helper")
    assert h.kind == "FUNCTION"


def test_rb_method_parent_id_links_to_class(rb_extracted) -> None:
    bar = next(s for s in rb_extracted.symbols if s.qualified_name == "Bar")
    greet = next(s for s in rb_extracted.symbols if s.qualified_name == "Bar.greet")
    assert greet.parent_id == bar.id


def test_rb_require_becomes_import_relation(rb_extracted) -> None:
    """``require "json"`` is syntactically a call but semantically an
    import; the extractor detects the special method name and emits
    an import relation with the string argument as target.
    """
    imports = sorted(
        r.target_name for r in rb_extracted.relations if r.kind == "imports"
    )
    assert imports == ["json", "uri"]


def test_rb_call_inside_method_source_qname(rb_extracted) -> None:
    """``helper(name)`` inside ``Bar.greet`` body has
    source_qname='Bar.greet'.
    """
    call = next(
        r for r in rb_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "Bar.greet"
    )
    assert call is not None


def test_rb_same_line_duplicate_call(rb_extracted) -> None:
    """``helper(1); helper(2)`` at module level → two relations
    differing only in column_start.
    """
    module_calls = [
        r for r in rb_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == ""
    ]
    last_line = max(r.line for r in module_calls)
    same_line = [r for r in module_calls if r.line == last_line]
    assert len(same_line) == 2
    assert same_line[0].column_start != same_line[1].column_start


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def php_extracted():
    parser = tsp.get_parser("php")
    return extract(
        parser, "php",
        (FIXTURES_DIR / "sample.php").read_bytes(),
        "sample.php",
    )


def test_php_no_error_nodes(php_extracted) -> None:
    assert php_extracted.had_error_nodes is False


def test_php_symbol_set(php_extracted) -> None:
    """Greeter CLASS + 2 methods, Animal INTERFACE + interface method,
    helper top-level FUNCTION. ``namespace App\\Service;`` does NOT
    contribute a symbol (matches Java/C++ convention).
    """
    qnames = sorted(s.qualified_name for s in php_extracted.symbols)
    assert qnames == sorted([
        "Greeter", "Greeter.greet", "Greeter.helper",
        "Animal", "Animal.name",
        "helper",
    ])


def test_php_class_kind(php_extracted) -> None:
    g = next(s for s in php_extracted.symbols if s.qualified_name == "Greeter")
    assert g.kind == "CLASS"


def test_php_interface_kind(php_extracted) -> None:
    a = next(s for s in php_extracted.symbols if s.qualified_name == "Animal")
    assert a.kind == "INTERFACE"


def test_php_method_kind_and_parent_id(php_extracted) -> None:
    g = next(s for s in php_extracted.symbols if s.qualified_name == "Greeter")
    greet = next(
        s for s in php_extracted.symbols if s.qualified_name == "Greeter.greet"
    )
    assert greet.kind == "METHOD"
    assert greet.parent_id == g.id


def test_php_interface_method_is_method(php_extracted) -> None:
    n = next(
        s for s in php_extracted.symbols if s.qualified_name == "Animal.name"
    )
    assert n.kind == "METHOD"
    assert n.parent_qname == "Animal"


def test_php_top_level_function_is_function(php_extracted) -> None:
    """``function helper(int $x): int { ... }`` at file scope is
    FUNCTION even though there's also ``Greeter.helper`` METHOD.
    """
    h = next(s for s in php_extracted.symbols if s.qualified_name == "helper")
    assert h.kind == "FUNCTION"


def test_php_namespace_use_imports(php_extracted) -> None:
    r"""``use App\Repo\UserRepo;`` → target='App\Repo\UserRepo' (the
    qualified_name text preserved with PHP's backslash separator).
    """
    imports = sorted(
        r.target_name for r in php_extracted.relations if r.kind == "imports"
    )
    assert imports == ["App\\Repo\\UserRepo", "App\\Util\\Foo"]


def test_php_call_inside_method_source_qname(php_extracted) -> None:
    """``$this->helper(1)`` inside Greeter.greet has
    source_qname='Greeter.greet'.
    """
    member_calls = [
        r for r in php_extracted.relations
        if r.kind == "calls" and r.source_qname == "Greeter.greet"
    ]
    assert len(member_calls) >= 1
    # The target reflects PHP's `$this->helper` shape.
    assert any("helper" in r.target_name for r in member_calls)


# ---------------------------------------------------------------------------
# C
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def c_extracted():
    parser = tsp.get_parser("c")
    return extract(
        parser, "c",
        (FIXTURES_DIR / "sample.c").read_bytes(),
        "sample.c",
    )


def test_c_no_error_nodes(c_extracted) -> None:
    assert c_extracted.had_error_nodes is False


def test_c_symbol_set(c_extracted) -> None:
    qnames = sorted(s.qualified_name for s in c_extracted.symbols)
    assert qnames == sorted(["Point", "helper", "main"])


def test_c_struct_kind(c_extracted) -> None:
    p = next(s for s in c_extracted.symbols if s.qualified_name == "Point")
    assert p.kind == "STRUCT"


def test_c_includes_distinguish_system_and_quote(c_extracted) -> None:
    """``#include <stdio.h>`` → target='stdio.h' (brackets stripped).
    ``#include "local.h"`` → target='local.h' (quotes stripped).
    """
    imports = sorted(
        r.target_name for r in c_extracted.relations if r.kind == "imports"
    )
    assert imports == ["local.h", "stdio.h"]


def test_c_call_inside_main(c_extracted) -> None:
    main_calls = [
        r for r in c_extracted.relations
        if r.kind == "calls" and r.source_qname == "main"
    ]
    assert len(main_calls) == 2
    assert all(r.target_name == "helper" for r in main_calls)


# ---------------------------------------------------------------------------
# C++ (sample.cpp) — class methods + .h cohabitation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cpp_extracted():
    parser = tsp.get_parser("cpp")
    return extract(
        parser, "cpp",
        (FIXTURES_DIR / "cpp_with_header" / "sample.cpp").read_bytes(),
        "cpp_with_header/sample.cpp",
    )


def test_cpp_no_error_nodes(cpp_extracted) -> None:
    assert cpp_extracted.had_error_nodes is False


def test_cpp_symbol_set(cpp_extracted) -> None:
    qnames = sorted(s.qualified_name for s in cpp_extracted.symbols)
    assert qnames == sorted([
        "Point", "Point.distance", "helper", "main",
    ])


def test_cpp_class_kind(cpp_extracted) -> None:
    p = next(s for s in cpp_extracted.symbols if s.qualified_name == "Point")
    assert p.kind == "CLASS"


def test_cpp_method_inside_class_is_method_with_field_identifier_name(
    cpp_extracted,
) -> None:
    """C++ class methods use ``field_identifier`` as the name node
    (vs ``identifier`` for free functions). The extractor must accept
    both — the C++ ``distance`` method here exercises the
    field_identifier branch.
    """
    d = next(
        s for s in cpp_extracted.symbols if s.qualified_name == "Point.distance"
    )
    assert d.kind == "METHOD"
    assert d.parent_qname == "Point"


def test_cpp_method_parent_id_links_to_class(cpp_extracted) -> None:
    p = next(s for s in cpp_extracted.symbols if s.qualified_name == "Point")
    d = next(
        s for s in cpp_extracted.symbols if s.qualified_name == "Point.distance"
    )
    assert d.parent_id == p.id


def test_cpp_call_inside_method_source_qname(cpp_extracted) -> None:
    """``return helper(1);`` inside ``Point.distance`` body — source
    must attribute to the method, NOT to its enclosing class.
    """
    calls = [
        r for r in cpp_extracted.relations
        if r.kind == "calls"
        and r.target_name == "helper"
        and r.source_qname == "Point.distance"
    ]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# C++ header (sample.h) — exercises the .h cohabitation heuristic
# ---------------------------------------------------------------------------


def test_h_cohabitation_walker_resolves_to_cpp() -> None:
    """``cpp_with_header/sample.h`` sits next to ``sample.cpp`` — the
    walker's ``.h`` heuristic (T3) must promote the header from C to
    C++ via the sibling ``.cpp`` evidence, routing it to the cpp
    grammar and cpp query.
    """
    cpp_dir = FIXTURES_DIR / "cpp_with_header"
    rels = list(walk(cpp_dir, include_ignored=True))
    by_name = {rel.name: (lang, grammar, qid) for rel, lang, grammar, qid in rels}
    assert by_name["sample.cpp"] == ("cpp", "cpp", "cpp")
    # The crucial one — `.h` walks as cpp because of the sibling .cpp.
    assert by_name["sample.h"] == ("cpp", "cpp", "cpp")


def test_h_file_parses_with_cpp_grammar() -> None:
    """The header file ``sample.h``, dispatched as cpp via the walker
    heuristic, extracts via the cpp processor without ERROR nodes.
    """
    parser = tsp.get_parser("cpp")
    src = (FIXTURES_DIR / "cpp_with_header" / "sample.h").read_bytes()
    result = extract(parser, "cpp", src, "sample.h")
    assert result.had_error_nodes is False


def test_h_file_symbols_emitted_as_cpp() -> None:
    """The header declares ``struct Header`` and a prototype
    ``int helper(int x);``. Only the STRUCT is captured (prototypes
    are ``declaration`` nodes, not ``function_definition`` — captured
    in this v1 scope only when they have a body).
    """
    parser = tsp.get_parser("cpp")
    src = (FIXTURES_DIR / "cpp_with_header" / "sample.h").read_bytes()
    result = extract(parser, "cpp", src, "sample.h")
    qnames = sorted(s.qualified_name for s in result.symbols)
    assert qnames == ["Header"]
    header = next(s for s in result.symbols if s.qualified_name == "Header")
    assert header.kind == "STRUCT"


# ---------------------------------------------------------------------------
# Walker × extractor routing for the simple Group C extensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,language,grammar,query_id",
    [
        ("sample.rb",  "rb",  "ruby", "rb"),
        ("sample.php", "php", "php",  "php"),
        ("sample.c",   "c",   "c",    "c"),
    ],
)
def test_walker_routes_extension_to_grammar_and_query(
    filename: str, language: str, grammar: str, query_id: str,
) -> None:
    rels = list(walk(FIXTURES_DIR, include_ignored=True))
    by_name = {rel.name: (lang, gram, qid) for rel, lang, gram, qid in rels}
    assert by_name[filename] == (language, grammar, query_id)
