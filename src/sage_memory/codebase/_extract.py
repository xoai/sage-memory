"""Tree-sitter extraction.

Public API: :func:`extract` parses ``source_bytes`` with the given
parser, runs the ``.scm`` query identified by ``query_id``, and returns
an :class:`ExtractedFile` containing symbols + relations.

``query_id`` is decoupled from the grammar name so that ``.jsx``
(grammar ``tsx``, query ``js``) and ``.tsx`` (grammar ``tsx``, query
``tsx``) can share a parser but extract different shapes (T3 walker
output already supplies them as separate fields).

Per-language processors (only Python ships in T5) translate raw
tree-sitter captures into ``ExtractedSymbol`` / ``ExtractedRelation``
records. The processor walks each captured node's parent chain to
compute ``qualified_name`` and disambiguate ``METHOD`` from
``FUNCTION`` — that logic doesn't fit cleanly in S-expression
predicates and we want it to be uniform across all 10 languages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path


_QUERIES_DIR = Path(__file__).parent / "queries"


@dataclass
class ExtractedSymbol:
    id: str            # uuid hex, assigned at extract time
    name: str
    qualified_name: str
    kind: str          # FUNCTION | CLASS | METHOD
    line_start: int    # 1-indexed (matches editor line numbers)
    line_end: int      # 1-indexed
    signature: str | None
    parent_qname: str | None  # diagnostic-only — empty for siblings
    parent_id: str | None     # FK target — the parent ExtractedSymbol.id,
                              # disambiguates sibling-shadow nested fns
                              # where parent_qname alone is ambiguous


@dataclass
class ExtractedRelation:
    target_name: str
    kind: str          # imports | calls
    line: int          # 1-indexed
    column_start: int  # 0-indexed
    source_qname: str  # "" for module-level


@dataclass
class ExtractedFile:
    symbols: list[ExtractedSymbol] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    had_error_nodes: bool = False


# Cache the .scm source string per query_id. Compiling a Query is cheap
# (microseconds) but reading the file isn't free; pinning the string in
# memory keeps the hot loop pure-Python.
_QUERY_SOURCES: dict[str, str] = {}


def _get_query_source(query_id: str) -> str:
    if query_id not in _QUERY_SOURCES:
        path = _QUERIES_DIR / f"{query_id}.scm"
        _QUERY_SOURCES[query_id] = path.read_text(encoding="utf-8")
    return _QUERY_SOURCES[query_id]


def extract(
    parser,
    query_id: str,
    source_bytes: bytes,
    rel_path: str,
) -> ExtractedFile:
    """Parse ``source_bytes`` and emit symbols + relations.

    Parameters
    ----------
    parser:
        A tree-sitter ``Parser`` configured for the file's grammar.
        Must have a ``.language`` attribute (modern tree-sitter API).
    query_id:
        Selects the ``.scm`` query file. Must match a file in
        ``codebase/queries/``.
    source_bytes:
        File contents as bytes (no decode — tree-sitter operates on
        bytes natively).
    rel_path:
        Repo-relative path of the file. Reserved for future cross-file
        resolution (e.g., module-path inference); not yet used by the
        Python processor.
    """
    processor = _PROCESSORS.get(query_id)
    if processor is None:
        raise NotImplementedError(
            f"Extraction for query_id={query_id!r} is not yet implemented "
            f"(T6-T8 ship the remaining languages)."
        )

    from tree_sitter import Query, QueryCursor

    tree = parser.parse(source_bytes)
    query = Query(parser.language, _get_query_source(query_id))
    cursor = QueryCursor(query)
    matches = cursor.matches(tree.root_node)
    symbols, relations = processor(matches)

    # Deterministic order regardless of how the query engine iterates.
    symbols.sort(key=lambda s: (s.line_start, s.qualified_name))
    relations.sort(key=lambda r: (r.line, r.column_start, r.target_name))

    return ExtractedFile(
        symbols=symbols,
        relations=relations,
        had_error_nodes=tree.root_node.has_error,
    )


# ───────────────────────────────────────────────────────────────────
# Per-language processor registry
# ───────────────────────────────────────────────────────────────────
#
# Defined at the bottom of the module after each ``_process_<lang>``
# function exists. ``query_id`` (not grammar name) keys the dispatch
# so ``.jsx`` (grammar=tsx, query=js) and ``.tsx`` (grammar=tsx,
# query=tsx) route to the right processor.

_PROCESSORS: dict[str, callable] = {}


# ───────────────────────────────────────────────────────────────────
# Python processor
# ───────────────────────────────────────────────────────────────────


def _process_python(matches) -> tuple[list[ExtractedSymbol], list[ExtractedRelation]]:
    # Collect symbol-defining and relation-defining captures separately
    # so symbols can be processed in source order (parent-first), which
    # the parent_id linkage logic depends on.
    symbol_captures: list[tuple[str, object]] = []  # (kind_hint, node)
    relation_captures: list[tuple[str, object]] = []

    for _pattern_id, captures in matches:
        for node in captures.get("function", []):
            symbol_captures.append(("function", node))
        for node in captures.get("class", []):
            symbol_captures.append(("class", node))
        for node in captures.get("import", []):
            relation_captures.append(("import", node))
        for node in captures.get("import_from", []):
            relation_captures.append(("import_from", node))
        for node in captures.get("call", []):
            relation_captures.append(("call", node))

    # Process symbols in source-position order so each child's tree
    # ancestor is already registered in node_to_symbol_id by the time
    # we need to look up its parent_id.
    symbol_captures.sort(key=lambda t: (t[1].start_point[0], t[1].start_point[1]))

    # tree-sitter Node objects hash by underlying node identity, so
    # different `.parent` wrappers referring to the same underlying
    # node collide on the same dict slot. This is how we look up an
    # ancestor symbol's id without keeping the original wrapper.
    node_to_symbol_id: dict[object, str] = {}
    symbols: list[ExtractedSymbol] = []
    for kind_hint, node in symbol_captures:
        sym = _build_symbol(node, kind_hint, node_to_symbol_id)
        symbols.append(sym)
        node_to_symbol_id[node] = sym.id

    relations: list[ExtractedRelation] = []
    for rel_kind, node in relation_captures:
        if rel_kind == "import":
            relations.extend(_build_import_relations(node))
        elif rel_kind == "import_from":
            relations.extend(_build_import_from_relations(node))
        elif rel_kind == "call":
            r = _build_call_relation(node)
            if r is not None:
                relations.append(r)

    return symbols, relations


def _build_symbol(
    node, kind_hint: str, node_to_symbol_id: dict[object, str],
) -> ExtractedSymbol:
    """Build an ExtractedSymbol, resolving parent_id by walking
    tree-sitter parents until we hit a node already registered as a
    symbol. This is the only correct way to disambiguate sibling-shadow
    nested functions: two ``def outer():`` siblings each with
    ``def inner_helper()`` share qname='outer.inner_helper' but have
    distinct parent_ids that point to the right ``outer``.
    """
    name_node = node.child_by_field_name("name")
    name = name_node.text.decode("utf-8") if name_node is not None else "<anonymous>"
    chain = _enclosing_symbol_chain(node)

    if kind_hint == "class":
        kind = "CLASS"
    else:
        # METHOD only when the *immediate* enclosing symbol is a class.
        # A function defined inside a method is still a FUNCTION.
        kind = "METHOD" if chain and chain[-1][0] == "CLASS" else "FUNCTION"

    parent_id: str | None = None
    cur = node.parent
    while cur is not None:
        sid = node_to_symbol_id.get(cur)
        if sid is not None:
            parent_id = sid
            break
        cur = cur.parent

    return ExtractedSymbol(
        id=uuid.uuid4().hex,
        name=name,
        qualified_name=_join_qname(chain, name),
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=_parent_qname(chain),
        parent_id=parent_id,
    )


def _enclosing_symbol_chain(node) -> list[tuple[str, str]]:
    """Walk ``node.parent`` upward, returning ``(kind, name)`` pairs for
    each enclosing ``function_definition`` / ``class_definition``,
    outermost first.
    """
    chain: list[tuple[str, str]] = []
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            name_node = cur.child_by_field_name("name")
            if name_node is not None:
                chain.append(("FUNCTION", name_node.text.decode("utf-8")))
        elif cur.type == "class_definition":
            name_node = cur.child_by_field_name("name")
            if name_node is not None:
                chain.append(("CLASS", name_node.text.decode("utf-8")))
        cur = cur.parent
    chain.reverse()
    return chain


def _join_qname(chain: list[tuple[str, str]], leaf: str) -> str:
    if not chain:
        return leaf
    return ".".join(n for _, n in chain) + "." + leaf


def _parent_qname(chain: list[tuple[str, str]]) -> str | None:
    return ".".join(n for _, n in chain) or None


def _source_qname(chain: list[tuple[str, str]]) -> str:
    """Source-side qualified name for a relation: empty string at
    module scope, else the innermost enclosing symbol's qname.
    """
    return ".".join(n for _, n in chain)


def _build_import_relations(node) -> list[ExtractedRelation]:
    """``import a, b.c, d as e`` → one relation per imported name."""
    source = _source_qname(_enclosing_symbol_chain(node))
    out: list[ExtractedRelation] = []
    for name_node in node.children_by_field_name("name"):
        target_node = _resolve_import_target(name_node)
        if target_node is None:
            continue
        out.append(ExtractedRelation(
            target_name=target_node.text.decode("utf-8"),
            kind="imports",
            line=target_node.start_point[0] + 1,
            column_start=target_node.start_point[1],
            source_qname=source,
        ))
    return out


def _build_import_from_relations(node) -> list[ExtractedRelation]:
    """``from m import a, b as c`` → one relation per imported name with
    target ``m.a``, ``m.b``, etc. Relative imports (``from . import x``)
    record the imported name only — resolution happens in T9a.
    """
    module_node = node.child_by_field_name("module_name")
    module = module_node.text.decode("utf-8") if module_node is not None else ""
    source = _source_qname(_enclosing_symbol_chain(node))
    out: list[ExtractedRelation] = []
    for name_node in node.children_by_field_name("name"):
        target_node = _resolve_import_target(name_node)
        if target_node is None:
            continue
        local = target_node.text.decode("utf-8")
        target = f"{module}.{local}" if module else local
        out.append(ExtractedRelation(
            target_name=target,
            kind="imports",
            line=target_node.start_point[0] + 1,
            column_start=target_node.start_point[1],
            source_qname=source,
        ))
    return out


def _resolve_import_target(name_node):
    """Given a node from ``children_by_field_name('name')`` on an
    import statement, return the ``dotted_name`` node that names the
    actual import target — unwrapping ``aliased_import`` if needed.
    Returns ``None`` for malformed shapes.
    """
    if name_node.type == "dotted_name":
        return name_node
    if name_node.type == "aliased_import":
        orig = name_node.child_by_field_name("name")
        if orig is not None and orig.type == "dotted_name":
            return orig
    return None


def _build_call_relation(call_node) -> ExtractedRelation | None:
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    target = _dotted_name(fn)
    if target is None:
        # Calls like ``obj().method()`` or ``(lambda: ...)()`` — no
        # stable dotted name. Recorded as unresolved in T9a; skipped
        # here so the relation set stays clean.
        return None
    chain = _enclosing_symbol_chain(call_node)
    return ExtractedRelation(
        target_name=target,
        kind="calls",
        line=fn.start_point[0] + 1,
        column_start=fn.start_point[1],
        source_qname=_source_qname(chain),
    )


def _dotted_name(node) -> str | None:
    """Render an ``identifier`` or chained ``attribute`` node as a
    dotted string. Returns ``None`` for shapes that don't have a
    stable name (e.g., ``attribute`` whose object is a call).
    """
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        obj_name = _dotted_name(obj) if obj is not None else None
        attr_name = (
            attr.text.decode("utf-8")
            if attr is not None and attr.type == "identifier"
            else None
        )
        if obj_name and attr_name:
            return f"{obj_name}.{attr_name}"
    return None


def _signature_first_line(node) -> str:
    """First line of the def header, e.g. ``def helper(x)`` or
    ``class Greeter``. Trailing ``:`` and whitespace are stripped so
    UI tooling can append its own colon / suffix consistently.
    """
    first = node.text.split(b"\n", 1)[0]
    return first.decode("utf-8", errors="replace").rstrip(":").rstrip()


_PROCESSORS["py"] = _process_python


# ───────────────────────────────────────────────────────────────────
# TypeScript / JavaScript / TSX processor
# ───────────────────────────────────────────────────────────────────
#
# All three query_ids share a processor — the language difference is in
# what each .scm captures (ts/tsx capture interface_declaration +
# enum_declaration; js does not). Walker routing (T3) supplies the
# right query_id per extension:
#
#   .ts  → grammar=typescript, query_id=ts
#   .tsx → grammar=tsx,        query_id=tsx
#   .jsx → grammar=tsx,        query_id=js   (js query against tsx grammar)
#   .js  → grammar=javascript, query_id=js


_TS_SYMBOL_KIND_MAP = {
    "function":  "FUNCTION",
    "method":    "METHOD",
    "class":     "CLASS",
    "interface": "INTERFACE",
    "enum":      "ENUM",
}


def _process_typescript(matches):
    """Build symbols + relations from TS/JS/TSX captures.

    The match dict can contain any subset of: ``function``, ``method``,
    ``class``, ``interface``, ``enum``, ``var_decl`` + ``var_name`` +
    ``var_value``, ``import``, ``call``. Each capture is processed
    independently; a single match may carry several (e.g. the named
    arrow-function pattern carries var_decl + var_name + var_value
    together).
    """
    # (kind, node, name_override) — name_override populated only for
    # the variable-bound arrow/function-expression case where the name
    # lives in a separate capture rather than the symbol node itself.
    symbol_captures: list[tuple[str, object, str | None]] = []
    relation_captures: list[tuple[str, object]] = []

    for _pid, captures in matches:
        for cap_kind in ("function", "method", "class", "interface", "enum"):
            for node in captures.get(cap_kind, []):
                symbol_captures.append((cap_kind, node, None))

        # Named arrow / function expression bound via lexical_declaration.
        # The symbol's *position* and *scope* live on the var_decl
        # (the surrounding `const x = ...`); the *name* lives on the
        # var_name identifier; the *body* lives on var_value (which we
        # use as the parent-walking key so nested fns find this fn).
        for i, decl_node in enumerate(captures.get("var_decl", [])):
            name_nodes = captures.get("var_name", [])
            value_nodes = captures.get("var_value", [])
            if i < len(name_nodes) and i < len(value_nodes):
                name = name_nodes[i].text.decode("utf-8")
                symbol_captures.append(("var_decl", decl_node, name))

        for node in captures.get("import", []):
            relation_captures.append(("import", node))
        for node in captures.get("call", []):
            relation_captures.append(("call", node))

    # Source-order processing so parent symbols register before
    # children look them up.
    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []
    for cap_kind, node, name_override in symbol_captures:
        sym = _build_ts_symbol(node, cap_kind, name_override, node_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        # For variable-bound functions we register BOTH the
        # variable_declarator (so its position is the enclosing
        # scope's start) AND the arrow/function_expression body (so
        # nested fns inside the body resolve correctly).
        node_to_symbol[node] = sym
        if cap_kind == "var_decl":
            # The body lives at value field; register it too.
            value_node = node.child_by_field_name("value")
            if value_node is not None:
                node_to_symbol[value_node] = sym

    relations: list[ExtractedRelation] = []
    for rel_kind, node in relation_captures:
        chain = _ts_enclosing_chain(node, node_to_symbol)
        source_qname = ".".join(s.name for s in chain)
        if rel_kind == "import":
            relations.extend(_build_ts_import_relations(node, source_qname))
        elif rel_kind == "call":
            r = _build_ts_call_relation(node, source_qname)
            if r is not None:
                relations.append(r)

    return symbols, relations


def _ts_enclosing_chain(
    node, node_to_symbol: dict[object, ExtractedSymbol],
) -> list[ExtractedSymbol]:
    """Walk node.parent up, collecting registered enclosing symbols
    outermost-first. Anonymous arrows / IIFEs are not in
    ``node_to_symbol`` so they don't contribute to the chain — which
    is exactly what we want for ``source_qname`` attribution.
    """
    chain: list[ExtractedSymbol] = []
    cur = node.parent
    while cur is not None:
        sym = node_to_symbol.get(cur)
        if sym is not None:
            chain.append(sym)
        cur = cur.parent
    chain.reverse()
    return chain


def _build_ts_symbol(
    node, cap_kind: str, name_override: str | None,
    node_to_symbol: dict[object, ExtractedSymbol],
) -> ExtractedSymbol | None:
    if name_override is not None:
        name = name_override
    else:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")

    chain = _ts_enclosing_chain(node, node_to_symbol)

    if cap_kind == "method":
        kind = "METHOD"
    elif cap_kind == "var_decl":
        # Named arrow / function expression. METHOD only if the
        # immediate enclosing symbol is a class (rare in TS — usually
        # methods use `method_definition` syntax, not `name = () => ...`).
        kind = "METHOD" if chain and chain[-1].kind == "CLASS" else "FUNCTION"
    else:
        kind = _TS_SYMBOL_KIND_MAP[cap_kind]

    parent_id = chain[-1].id if chain else None
    qualified_name = (
        ".".join(s.name for s in chain) + "." + name if chain else name
    )
    parent_qname = ".".join(s.name for s in chain) or None

    return ExtractedSymbol(
        id=uuid.uuid4().hex,
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=parent_qname,
        parent_id=parent_id,
    )


def _ts_import_source(node) -> str:
    """Return the unquoted module path from an ``import_statement``,
    or empty string for malformed shapes.
    """
    source = node.child_by_field_name("source")
    if source is None:
        return ""
    for child in source.children:
        if child.type == "string_fragment":
            return child.text.decode("utf-8")
    return ""


def _build_ts_import_relations(node, source_qname: str) -> list[ExtractedRelation]:
    """Translate one ``import_statement`` into one or more relations.

    - ``import { foo, bar as b } from "./mod"``
        → ``./mod.foo`` + ``./mod.bar`` (original names, alias dropped)
    - ``import * as ns from "./mod"`` → ``./mod`` (whole-module import)
    - ``import foo from "./mod"`` → ``./mod.default`` (the default
      export is conventionally named ``default``)
    - ``import "./mod"`` (side-effect only) → ``./mod``
    """
    module = _ts_import_source(node)
    if not module:
        return []

    clause = next(
        (c for c in node.children if c.type == "import_clause"),
        None,
    )
    out: list[ExtractedRelation] = []
    line = node.start_point[0] + 1
    column = node.start_point[1]

    if clause is None:
        # Side-effect import: target is the module itself.
        out.append(ExtractedRelation(
            target_name=module, kind="imports",
            line=line, column_start=column, source_qname=source_qname,
        ))
        return out

    for child in clause.children:
        if child.type == "identifier":
            # Default import: `import foo from "./bar"` → bar.default
            out.append(ExtractedRelation(
                target_name=f"{module}.default", kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname=source_qname,
            ))
        elif child.type == "namespace_import":
            # `import * as ns from "./bar"` → bar (whole module)
            out.append(ExtractedRelation(
                target_name=module, kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname=source_qname,
            ))
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                if name_node is None:
                    continue
                local_name = name_node.text.decode("utf-8")
                out.append(ExtractedRelation(
                    target_name=f"{module}.{local_name}", kind="imports",
                    line=name_node.start_point[0] + 1,
                    column_start=name_node.start_point[1],
                    source_qname=source_qname,
                ))
    return out


def _build_ts_call_relation(call_node, source_qname: str) -> ExtractedRelation | None:
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    target = _ts_dotted_name(fn)
    if target is None:
        return None
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=fn.start_point[0] + 1, column_start=fn.start_point[1],
        source_qname=source_qname,
    )


def _ts_dotted_name(node) -> str | None:
    """Render an identifier or member_expression chain as a dotted
    name. Returns None for shapes without a stable name (call result,
    parenthesised expression, ``this``, etc.).
    """
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        obj_name = _ts_dotted_name(obj) if obj is not None else None
        prop_name = (
            prop.text.decode("utf-8")
            if prop is not None and prop.type == "property_identifier"
            else None
        )
        if obj_name and prop_name:
            return f"{obj_name}.{prop_name}"
    return None


_PROCESSORS["ts"] = _process_typescript
_PROCESSORS["tsx"] = _process_typescript
_PROCESSORS["js"] = _process_typescript


# ───────────────────────────────────────────────────────────────────
# Shared helpers for Group B (Go / Rust / Java)
# ───────────────────────────────────────────────────────────────────


def _walk_symbol_chain(
    node, node_to_symbol: dict[object, ExtractedSymbol],
) -> list[ExtractedSymbol]:
    """Walk node.parent up, returning registered enclosing symbols
    outermost-first. Generic across languages — works for any
    processor that maps a node to an ExtractedSymbol.
    """
    chain: list[ExtractedSymbol] = []
    cur = node.parent
    while cur is not None:
        sym = node_to_symbol.get(cur)
        if sym is not None:
            chain.append(sym)
        cur = cur.parent
    chain.reverse()
    return chain


# ───────────────────────────────────────────────────────────────────
# Go processor
# ───────────────────────────────────────────────────────────────────


def _go_receiver_type(method_decl) -> str | None:
    """Extract the receiver-type name from a ``method_declaration``.
    Handles both value-receiver (``(p Person)``) and pointer-receiver
    (``(p *Person)``).
    """
    recv = method_decl.child_by_field_name("receiver")
    if recv is None:
        return None
    for param in recv.children:
        if param.type != "parameter_declaration":
            continue
        type_node = param.child_by_field_name("type")
        if type_node is None:
            continue
        if type_node.type == "pointer_type":
            for sub in type_node.children:
                if sub.type == "type_identifier":
                    return sub.text.decode("utf-8")
        elif type_node.type == "type_identifier":
            return type_node.text.decode("utf-8")
    return None


def _process_go(matches):
    symbol_captures: list[tuple[str, object]] = []
    relation_captures: list[tuple[str, object]] = []

    for _pid, captures in matches:
        for n in captures.get("function", []):
            symbol_captures.append(("function", n))
        for n in captures.get("method", []):
            symbol_captures.append(("method", n))
        # type_declaration wraps one or more type_specs. Emit a STRUCT
        # symbol per type_spec whose type is struct_type. Other kinds
        # (interface_type, alias) are skipped for v1.
        for type_decl in captures.get("type_decl", []):
            for child in type_decl.children:
                if child.type != "type_spec":
                    continue
                type_kind_node = child.child_by_field_name("type")
                if type_kind_node is None:
                    continue
                if type_kind_node.type == "struct_type":
                    symbol_captures.append(("struct", child))
        for n in captures.get("import", []):
            relation_captures.append(("import", n))
        for n in captures.get("call", []):
            relation_captures.append(("call", n))

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    qname_to_symbol: dict[str, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []

    for cap_kind, node in symbol_captures:
        sym = _build_go_symbol(node, cap_kind, qname_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym
        qname_to_symbol[sym.qualified_name] = sym

    relations: list[ExtractedRelation] = []
    for rel_kind, node in relation_captures:
        chain = _walk_symbol_chain(node, node_to_symbol)
        source_qname = ".".join(s.qualified_name for s in chain[-1:])
        if rel_kind == "import":
            relations.extend(_build_go_import_relations(node, source_qname))
        elif rel_kind == "call":
            r = _build_go_call_relation(node, source_qname)
            if r is not None:
                relations.append(r)

    return symbols, relations


def _build_go_symbol(node, cap_kind: str, qname_to_symbol):
    if cap_kind == "function":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=name,
            kind="FUNCTION",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=_signature_first_line(node),
            parent_qname=None, parent_id=None,
        )
    if cap_kind == "method":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        recv_type = _go_receiver_type(node)
        qname = f"{recv_type}.{name}" if recv_type else name
        # parent_id: if the receiver type was declared in this file
        # (and has been emitted as a STRUCT), link to it. Otherwise
        # leave NULL — cross-file resolution is T9b's problem.
        parent_id = (
            qname_to_symbol[recv_type].id
            if recv_type and recv_type in qname_to_symbol
            else None
        )
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=qname,
            kind="METHOD",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=_signature_first_line(node),
            parent_qname=recv_type, parent_id=parent_id,
        )
    if cap_kind == "struct":
        # node is the type_spec; its name is a type_identifier.
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=name,
            kind="STRUCT",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=f"type {name} struct",
            parent_qname=None, parent_id=None,
        )
    return None


def _build_go_import_relations(node, source_qname: str) -> list[ExtractedRelation]:
    """``import "fmt"`` → one relation. ``import (...)`` → one per spec."""
    out: list[ExtractedRelation] = []
    # Either inline `import "x"` (single import_spec child) or block
    # `import (...)` with an import_spec_list of import_specs.
    specs = []
    for child in node.children:
        if child.type == "import_spec":
            specs.append(child)
        elif child.type == "import_spec_list":
            for sub in child.children:
                if sub.type == "import_spec":
                    specs.append(sub)
    for spec in specs:
        path_node = spec.child_by_field_name("path")
        if path_node is None:
            continue
        target = path_node.text.decode("utf-8").strip('"')
        out.append(ExtractedRelation(
            target_name=target, kind="imports",
            line=spec.start_point[0] + 1,
            column_start=spec.start_point[1],
            source_qname=source_qname,
        ))
    return out


def _build_go_call_relation(call_node, source_qname: str):
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    target = _go_call_target_name(fn)
    if target is None:
        return None
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=fn.start_point[0] + 1, column_start=fn.start_point[1],
        source_qname=source_qname,
    )


def _go_call_target_name(node) -> str | None:
    """Go calls: identifier, selector_expression (a.b), or
    package.symbol (which is also selector_expression).
    """
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        op_name = _go_call_target_name(operand) if operand else None
        field_name = field.text.decode("utf-8") if field is not None else None
        if op_name and field_name:
            return f"{op_name}.{field_name}"
    return None


_PROCESSORS["go"] = _process_go


# ───────────────────────────────────────────────────────────────────
# Rust processor
# ───────────────────────────────────────────────────────────────────


def _process_rust(matches):
    impl_captures: list[object] = []
    symbol_captures: list[tuple[str, object]] = []
    relation_captures: list[tuple[str, object]] = []

    for _pid, captures in matches:
        for n in captures.get("impl", []):
            impl_captures.append(n)
        for cap in ("function", "struct", "enum"):
            for n in captures.get(cap, []):
                symbol_captures.append((cap, n))
        for n in captures.get("use", []):
            relation_captures.append(("use", n))
        for n in captures.get("call", []):
            relation_captures.append(("call", n))

    # Map impl_item → type name (e.g., "Point") so methods inside the
    # impl can name their receiver. impl_item is NOT itself a symbol —
    # the struct it impls IS — so we don't emit a row for it.
    impl_to_type: dict[object, str] = {}
    for impl_node in impl_captures:
        type_node = impl_node.child_by_field_name("type")
        if type_node is None:
            continue
        if type_node.type in ("type_identifier", "scoped_type_identifier"):
            impl_to_type[impl_node] = type_node.text.decode("utf-8")

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    qname_to_symbol: dict[str, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []

    for cap_kind, node in symbol_captures:
        sym = _build_rust_symbol(node, cap_kind, impl_to_type, qname_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym
        qname_to_symbol[sym.qualified_name] = sym

    relations: list[ExtractedRelation] = []
    for rel_kind, node in relation_captures:
        chain = _walk_symbol_chain(node, node_to_symbol)
        # For Rust, the innermost SYMBOL is the call's source, regardless
        # of whether there's an impl_item between the call and the symbol
        # (impl isn't a symbol). The function_item registered as a
        # METHOD/FUNCTION is the right anchor.
        source_qname = chain[-1].qualified_name if chain else ""
        if rel_kind == "use":
            r = _build_rust_use_relation(node, source_qname)
            if r is not None:
                relations.append(r)
        elif rel_kind == "call":
            r = _build_rust_call_relation(node, source_qname)
            if r is not None:
                relations.append(r)

    return symbols, relations


def _build_rust_symbol(node, cap_kind, impl_to_type, qname_to_symbol):
    if cap_kind == "function":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        # Walk up to find an impl_item ancestor. function_item inside
        # an impl is a METHOD on the impl's type.
        impl_type = None
        cur = node.parent
        while cur is not None:
            if cur.type == "impl_item":
                impl_type = impl_to_type.get(cur)
                break
            if cur.type == "function_item":
                # nested fn (closure-style) — still FUNCTION, not METHOD.
                break
            cur = cur.parent
        if impl_type is not None:
            qname = f"{impl_type}.{name}"
            parent_id = (
                qname_to_symbol[impl_type].id
                if impl_type in qname_to_symbol else None
            )
            return ExtractedSymbol(
                id=uuid.uuid4().hex, name=name, qualified_name=qname,
                kind="METHOD",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                signature=_signature_first_line(node),
                parent_qname=impl_type, parent_id=parent_id,
            )
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=name,
            kind="FUNCTION",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=_signature_first_line(node),
            parent_qname=None, parent_id=None,
        )
    if cap_kind == "struct":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=name,
            kind="STRUCT",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=_signature_first_line(node),
            parent_qname=None, parent_id=None,
        )
    if cap_kind == "enum":
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        return ExtractedSymbol(
            id=uuid.uuid4().hex, name=name, qualified_name=name,
            kind="ENUM",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=_signature_first_line(node),
            parent_qname=None, parent_id=None,
        )
    return None


def _build_rust_use_relation(node, source_qname: str):
    """Capture single-path uses like ``use a::b::c;``. Brace-grouped
    use lists (``use a::{b, c}``) are skipped for v1 — they appear in
    only a fraction of real codebases and full handling is T9b's
    resolution problem.
    """
    for child in node.children:
        if child.type == "scoped_identifier":
            target = child.text.decode("utf-8")
            return ExtractedRelation(
                target_name=target, kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname=source_qname,
            )
        if child.type == "identifier":
            return ExtractedRelation(
                target_name=child.text.decode("utf-8"), kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname=source_qname,
            )
    return None


def _build_rust_call_relation(call_node, source_qname: str):
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    target = _rust_call_target(fn)
    if target is None:
        return None
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=fn.start_point[0] + 1, column_start=fn.start_point[1],
        source_qname=source_qname,
    )


def _rust_call_target(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "scoped_identifier":
        # `Point::new` — emit as-is (with ::)
        return node.text.decode("utf-8")
    if node.type == "field_expression":
        value = node.child_by_field_name("value")
        field = node.child_by_field_name("field")
        v_name = _rust_call_target(value) if value is not None else None
        f_name = (
            field.text.decode("utf-8")
            if field is not None and field.type == "field_identifier"
            else None
        )
        if v_name and f_name:
            return f"{v_name}.{f_name}"
    return None


_PROCESSORS["rs"] = _process_rust


# ───────────────────────────────────────────────────────────────────
# Java processor
# ───────────────────────────────────────────────────────────────────


_JAVA_KIND_MAP = {
    "class": "CLASS",
    "interface": "INTERFACE",
    "enum": "ENUM",
    "method": "METHOD",
}


def _process_java(matches):
    symbol_captures: list[tuple[str, object]] = []
    relation_captures: list[tuple[str, object]] = []

    for _pid, captures in matches:
        for cap_kind in ("class", "interface", "enum", "method"):
            for n in captures.get(cap_kind, []):
                symbol_captures.append((cap_kind, n))
        for n in captures.get("import", []):
            relation_captures.append(("import", n))
        for n in captures.get("call", []):
            relation_captures.append(("call", n))

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []
    for cap_kind, node in symbol_captures:
        sym = _build_java_symbol(node, cap_kind, node_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym

    relations: list[ExtractedRelation] = []
    for rel_kind, node in relation_captures:
        chain = _walk_symbol_chain(node, node_to_symbol)
        source_qname = chain[-1].qualified_name if chain else ""
        if rel_kind == "import":
            r = _build_java_import_relation(node, source_qname)
            if r is not None:
                relations.append(r)
        elif rel_kind == "call":
            r = _build_java_call_relation(node, source_qname)
            if r is not None:
                relations.append(r)

    return symbols, relations


def _build_java_symbol(node, cap_kind, node_to_symbol):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = name_node.text.decode("utf-8")
    chain = _walk_symbol_chain(node, node_to_symbol)
    parent_qname = chain[-1].qualified_name if chain else None
    parent_id = chain[-1].id if chain else None
    qname = f"{parent_qname}.{name}" if parent_qname else name
    return ExtractedSymbol(
        id=uuid.uuid4().hex, name=name, qualified_name=qname,
        kind=_JAVA_KIND_MAP[cap_kind],
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=parent_qname, parent_id=parent_id,
    )


def _build_java_import_relation(node, source_qname: str):
    # import_declaration has one scoped_identifier (or identifier) child
    # naming the imported symbol.
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            return ExtractedRelation(
                target_name=child.text.decode("utf-8"), kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname=source_qname,
            )
    return None


def _build_java_call_relation(call_node, source_qname: str):
    """``method_invocation`` shape: optional `object:` field +
    `name:` identifier + arguments. Target is ``object.name`` if
    object exists, else just ``name``.
    """
    name = call_node.child_by_field_name("name")
    if name is None:
        return None
    name_str = name.text.decode("utf-8")
    obj = call_node.child_by_field_name("object")
    if obj is not None:
        obj_name = _java_call_object_name(obj)
        target = f"{obj_name}.{name_str}" if obj_name else name_str
    else:
        target = name_str
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=name.start_point[0] + 1, column_start=name.start_point[1],
        source_qname=source_qname,
    )


def _java_call_object_name(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "field_access":
        obj = node.child_by_field_name("object")
        field = node.child_by_field_name("field")
        obj_name = _java_call_object_name(obj) if obj else None
        f_name = field.text.decode("utf-8") if field else None
        if obj_name and f_name:
            return f"{obj_name}.{f_name}"
    return None


_PROCESSORS["java"] = _process_java


# ───────────────────────────────────────────────────────────────────
# Ruby processor
# ───────────────────────────────────────────────────────────────────


# Ruby calls that should be treated as imports instead of regular calls.
_RUBY_IMPORT_METHODS = frozenset({"require", "require_relative", "load", "autoload"})


def _process_ruby(matches):
    symbol_captures: list[tuple[str, object]] = []
    call_captures: list[object] = []

    for _pid, captures in matches:
        for cap_kind in ("method", "class"):
            for n in captures.get(cap_kind, []):
                symbol_captures.append((cap_kind, n))
        for n in captures.get("call", []):
            call_captures.append(n)

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []
    for cap_kind, node in symbol_captures:
        sym = _build_ruby_symbol(node, cap_kind, node_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym

    relations: list[ExtractedRelation] = []
    for call_node in call_captures:
        chain = _walk_symbol_chain(call_node, node_to_symbol)
        source_qname = chain[-1].qualified_name if chain else ""
        method_node = call_node.child_by_field_name("method")
        # Skip nested `call` nodes that are themselves the receiver of
        # an outer call — we'd otherwise double-count `a.b` in `a.b.c`.
        # Tree-sitter exposes the inner `call` via the receiver field
        # of the outer call (`a.b.c` → outer.receiver = call(`a.b`)).
        parent = call_node.parent
        if parent is not None and parent.type == "call":
            if parent.child_by_field_name("receiver") is call_node:
                continue
        if method_node is None:
            continue
        method_name = method_node.text.decode("utf-8")
        # `require "x"` → import
        if method_name in _RUBY_IMPORT_METHODS:
            r = _build_ruby_require_relation(call_node, method_name, source_qname)
            if r is not None:
                relations.append(r)
            continue
        # Regular call — build dotted name from receiver chain.
        target = _ruby_call_target(call_node)
        if target is None:
            continue
        relations.append(ExtractedRelation(
            target_name=target, kind="calls",
            line=method_node.start_point[0] + 1,
            column_start=method_node.start_point[1],
            source_qname=source_qname,
        ))

    return symbols, relations


def _build_ruby_symbol(node, cap_kind, node_to_symbol):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = name_node.text.decode("utf-8")
    chain = _walk_symbol_chain(node, node_to_symbol)
    parent_qname = chain[-1].qualified_name if chain else None
    parent_id = chain[-1].id if chain else None
    qname = f"{parent_qname}.{name}" if parent_qname else name
    if cap_kind == "method":
        # METHOD only when the immediate enclosing symbol is a class.
        # Top-level ``def`` at module scope is FUNCTION.
        kind = "METHOD" if chain and chain[-1].kind == "CLASS" else "FUNCTION"
    else:
        kind = "CLASS"
    return ExtractedSymbol(
        id=uuid.uuid4().hex, name=name, qualified_name=qname,
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=parent_qname, parent_id=parent_id,
    )


def _build_ruby_require_relation(call_node, method_name, source_qname):
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        if arg.type == "string":
            for inner in arg.children:
                if inner.type == "string_content":
                    target = inner.text.decode("utf-8")
                    return ExtractedRelation(
                        target_name=target, kind="imports",
                        line=arg.start_point[0] + 1,
                        column_start=arg.start_point[1],
                        source_qname=source_qname,
                    )
    return None


def _ruby_call_target(call_node) -> str | None:
    """Render a Ruby call as a dotted name. Receiver may itself be a
    call (``a.b.c`` is parsed as a recursive call chain).
    """
    method = call_node.child_by_field_name("method")
    if method is None:
        return None
    method_name = method.text.decode("utf-8")
    receiver = call_node.child_by_field_name("receiver")
    if receiver is None:
        return method_name
    receiver_name = _ruby_receiver_name(receiver)
    if receiver_name is None:
        return method_name
    return f"{receiver_name}.{method_name}"


def _ruby_receiver_name(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "constant":
        return node.text.decode("utf-8")
    if node.type == "call":
        return _ruby_call_target(node)
    return None


_PROCESSORS["rb"] = _process_ruby


# ───────────────────────────────────────────────────────────────────
# PHP processor
# ───────────────────────────────────────────────────────────────────


_PHP_KIND_MAP = {
    "class": "CLASS",
    "interface": "INTERFACE",
    "method": "METHOD",
    "function": "FUNCTION",
}


def _process_php(matches):
    symbol_captures: list[tuple[str, object]] = []
    use_captures: list[object] = []
    call_captures: list[tuple[str, object]] = []

    for _pid, captures in matches:
        for cap_kind in ("class", "interface", "method", "function"):
            for n in captures.get(cap_kind, []):
                symbol_captures.append((cap_kind, n))
        for n in captures.get("use", []):
            use_captures.append(n)
        for kind, key in (("call", "call"), ("member_call", "member_call"),
                          ("scoped_call", "scoped_call")):
            for n in captures.get(key, []):
                call_captures.append((kind, n))

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []
    for cap_kind, node in symbol_captures:
        sym = _build_php_symbol(node, cap_kind, node_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym

    relations: list[ExtractedRelation] = []
    for use_node in use_captures:
        relations.extend(_build_php_use_relations(use_node))
    for kind, call_node in call_captures:
        chain = _walk_symbol_chain(call_node, node_to_symbol)
        source_qname = chain[-1].qualified_name if chain else ""
        r = _build_php_call_relation(kind, call_node, source_qname)
        if r is not None:
            relations.append(r)

    return symbols, relations


def _build_php_symbol(node, cap_kind, node_to_symbol):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return None
    name = name_node.text.decode("utf-8")
    chain = _walk_symbol_chain(node, node_to_symbol)
    parent_qname = chain[-1].qualified_name if chain else None
    parent_id = chain[-1].id if chain else None
    qname = f"{parent_qname}.{name}" if parent_qname else name
    kind = _PHP_KIND_MAP[cap_kind]
    # method_declaration always lives inside class/interface; mark as METHOD.
    if cap_kind == "method":
        kind = "METHOD"
    return ExtractedSymbol(
        id=uuid.uuid4().hex, name=name, qualified_name=qname,
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=parent_qname, parent_id=parent_id,
    )


def _build_php_use_relations(node) -> list[ExtractedRelation]:
    r"""``use App\Foo;`` → one relation. ``use App\Bar as B;`` → one
    relation (alias dropped). Function/const use prefixes are
    preserved in the target_name text as-is.
    """
    out: list[ExtractedRelation] = []
    for child in node.children:
        if child.type != "namespace_use_clause":
            continue
        for sub in child.children:
            if sub.type == "qualified_name":
                out.append(ExtractedRelation(
                    target_name=sub.text.decode("utf-8"), kind="imports",
                    line=sub.start_point[0] + 1,
                    column_start=sub.start_point[1],
                    source_qname="",
                ))
                break
    return out


def _build_php_call_relation(kind, call_node, source_qname):
    if kind == "call":
        # function_call_expression: function field + arguments field
        fn = call_node.child_by_field_name("function")
        if fn is None:
            return None
        target = fn.text.decode("utf-8")
        line = fn.start_point[0] + 1
        col = fn.start_point[1]
    elif kind == "member_call":
        obj = call_node.child_by_field_name("object")
        name_node = call_node.child_by_field_name("name")
        if name_node is None:
            return None
        obj_name = obj.text.decode("utf-8") if obj is not None else ""
        target = f"{obj_name}.{name_node.text.decode('utf-8')}" if obj_name else name_node.text.decode("utf-8")
        line = name_node.start_point[0] + 1
        col = name_node.start_point[1]
    elif kind == "scoped_call":
        scope = call_node.child_by_field_name("scope")
        name_node = call_node.child_by_field_name("name")
        if name_node is None:
            return None
        scope_name = scope.text.decode("utf-8") if scope is not None else ""
        target = f"{scope_name}::{name_node.text.decode('utf-8')}" if scope_name else name_node.text.decode("utf-8")
        line = name_node.start_point[0] + 1
        col = name_node.start_point[1]
    else:
        return None
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=line, column_start=col, source_qname=source_qname,
    )


_PROCESSORS["php"] = _process_php


# ───────────────────────────────────────────────────────────────────
# C / C++ processor (shared)
# ───────────────────────────────────────────────────────────────────


def _process_c_family(matches):
    """C and C++ share most of their tree structure; the difference is
    that C++ also has ``class_specifier`` which we capture as CLASS
    and which makes any contained ``function_definition`` a METHOD.
    """
    symbol_captures: list[tuple[str, object]] = []
    include_captures: list[object] = []
    call_captures: list[object] = []

    for _pid, captures in matches:
        for n in captures.get("function", []):
            symbol_captures.append(("function", n))
        for n in captures.get("class", []):
            symbol_captures.append(("class", n))
        for n in captures.get("struct", []):
            symbol_captures.append(("struct", n))
        for n in captures.get("include", []):
            include_captures.append(n)
        for n in captures.get("call", []):
            call_captures.append(n)

    symbol_captures.sort(
        key=lambda t: (t[1].start_point[0], t[1].start_point[1])
    )

    node_to_symbol: dict[object, ExtractedSymbol] = {}
    symbols: list[ExtractedSymbol] = []
    for cap_kind, node in symbol_captures:
        sym = _build_c_family_symbol(node, cap_kind, node_to_symbol)
        if sym is None:
            continue
        symbols.append(sym)
        node_to_symbol[node] = sym

    relations: list[ExtractedRelation] = []
    for inc in include_captures:
        r = _build_c_include_relation(inc)
        if r is not None:
            relations.append(r)
    for call_node in call_captures:
        chain = _walk_symbol_chain(call_node, node_to_symbol)
        source_qname = chain[-1].qualified_name if chain else ""
        r = _build_c_call_relation(call_node, source_qname)
        if r is not None:
            relations.append(r)

    return symbols, relations


def _c_function_name(fdef_node):
    """Walk function_definition.declarator → function_declarator →
    its `declarator` field (identifier).
    """
    decl = fdef_node.child_by_field_name("declarator")
    if decl is None:
        return None
    # Skip pointer_declarator and parenthesized_declarator wrappers if
    # any (e.g. ``int *foo()`` has pointer_declarator wrapping function_declarator).
    while decl.type != "function_declarator":
        inner = decl.child_by_field_name("declarator")
        if inner is None:
            return None
        decl = inner
    name_node = decl.child_by_field_name("declarator")
    if name_node is None:
        return None
    # ``identifier`` covers free C/C++ functions; ``field_identifier``
    # covers class/struct methods (tree-sitter uses a distinct node
    # type for member-context identifiers). Operator overloads and
    # destructors use other shapes (operator_name, destructor_name,
    # qualified_identifier for out-of-line definitions) — skipped for v1.
    if name_node.type not in ("identifier", "field_identifier"):
        return None
    return name_node


def _build_c_family_symbol(node, cap_kind, node_to_symbol):
    chain = _walk_symbol_chain(node, node_to_symbol)
    parent_qname = chain[-1].qualified_name if chain else None
    parent_id = chain[-1].id if chain else None

    if cap_kind == "function":
        name_node = _c_function_name(node)
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        # METHOD when the immediate enclosing symbol is a CLASS or STRUCT
        # (C++ allows methods in struct too). Otherwise FUNCTION.
        kind = (
            "METHOD"
            if chain and chain[-1].kind in ("CLASS", "STRUCT")
            else "FUNCTION"
        )
    else:
        # class_specifier or struct_specifier: name field is type_identifier
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None
        name = name_node.text.decode("utf-8")
        kind = "CLASS" if cap_kind == "class" else "STRUCT"

    qname = f"{parent_qname}.{name}" if parent_qname else name
    return ExtractedSymbol(
        id=uuid.uuid4().hex, name=name, qualified_name=qname,
        kind=kind,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=_signature_first_line(node),
        parent_qname=parent_qname, parent_id=parent_id,
    )


def _build_c_include_relation(node):
    """``#include <foo.h>`` → target='foo.h' (angle-bracket form)
    ``#include "foo.h"`` → target='foo.h' (quote form)
    """
    for child in node.children:
        if child.type == "system_lib_string":
            text = child.text.decode("utf-8").strip("<>")
            return ExtractedRelation(
                target_name=text, kind="imports",
                line=child.start_point[0] + 1,
                column_start=child.start_point[1],
                source_qname="",
            )
        if child.type == "string_literal":
            for sub in child.children:
                if sub.type == "string_content":
                    return ExtractedRelation(
                        target_name=sub.text.decode("utf-8"), kind="imports",
                        line=child.start_point[0] + 1,
                        column_start=child.start_point[1],
                        source_qname="",
                    )
    return None


def _build_c_call_relation(call_node, source_qname):
    fn = call_node.child_by_field_name("function")
    if fn is None:
        return None
    target = _c_call_target(fn)
    if target is None:
        return None
    return ExtractedRelation(
        target_name=target, kind="calls",
        line=fn.start_point[0] + 1, column_start=fn.start_point[1],
        source_qname=source_qname,
    )


def _c_call_target(node) -> str | None:
    if node.type == "identifier":
        return node.text.decode("utf-8")
    if node.type == "field_expression":
        # `obj.method` or `ptr->method`
        argument = node.child_by_field_name("argument")
        field = node.child_by_field_name("field")
        arg_name = _c_call_target(argument) if argument is not None else None
        f_name = (
            field.text.decode("utf-8")
            if field is not None and field.type == "field_identifier"
            else None
        )
        if arg_name and f_name:
            return f"{arg_name}.{f_name}"
    if node.type == "qualified_identifier":
        # `ns::name` or `Type::method`
        return node.text.decode("utf-8")
    return None


_PROCESSORS["c"] = _process_c_family
_PROCESSORS["cpp"] = _process_c_family
