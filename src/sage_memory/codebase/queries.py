"""Structural code-graph queries — P2-1 (SM-CAP-01).

Exposes the code graph persisted by ``scan-codebase``
(``code_symbols`` + ``code_relations``) through three deterministic
queries: ``find_path``, ``affected``, ``hubs``. No LLM, no
embeddings, no new dependencies — SQL + bounded in-memory BFS.

Design: ``docs/design/code-graph-queries.md``. Load-bearing rule:
``resolved`` edges (``target_symbol_id IS NOT NULL``) are facts;
``unresolved`` edges are name-matches — ``find_path`` uses resolved
edges only (a path through a guess is invented provenance), while
``affected``/``hubs`` include both, labelled, with a
``resolved_only`` filter. Every traversal is cycle-safe (visited
set, mirroring ``graph.py``) and cap-bounded with an honest
``truncated`` signal.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path
from typing import Any


# Caps per design brief — every cap that bites sets truncated: true.
_PATH_MAX_DEPTH = 16
_PATH_MAX_VISITED = 10_000
_AFFECTED_MAX_DEPTH = 8
_AFFECTED_MAX_RESULTS = 500


def _symbol_candidates(
    conn: sqlite3.Connection, name: str, limit: int = 20,
) -> list[dict[str, Any]]:
    """Resolve a user-supplied symbol reference to candidate rows.

    Exact ``qualified_name`` match first; bare ``name`` second.
    Returns rows with qualified_name, kind, file, line for
    disambiguation — never guesses.
    """
    rows = conn.execute(
        """SELECT cs.id, cs.name, cs.qualified_name, cs.kind,
                  cs.line_start, cb.file_path
           FROM code_symbols cs
           JOIN codebase_scans cb ON cb.file_memory_id = cs.file_memory_id
           WHERE cs.qualified_name = ? AND cs.kind != 'MODULE'
           LIMIT ?""",
        (name, limit),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """SELECT cs.id, cs.name, cs.qualified_name, cs.kind,
                      cs.line_start, cb.file_path
               FROM code_symbols cs
               JOIN codebase_scans cb
                 ON cb.file_memory_id = cs.file_memory_id
               WHERE cs.name = ? AND cs.kind != 'MODULE'
               LIMIT ?""",
            (name, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file": r["file_path"],
            "line": r["line_start"],
        }
        for r in rows
    ]


def _rel_path_for(conn: sqlite3.Connection, symbol_id: str) -> tuple[str, int]:
    row = conn.execute(
        """SELECT cb.file_path, cs.line_start
           FROM code_symbols cs
           JOIN codebase_scans cb ON cb.file_memory_id = cs.file_memory_id
           WHERE cs.id = ?""",
        (symbol_id,),
    ).fetchone()
    return (row["file_path"], row["line_start"]) if row else ("", 0)


def find_path(
    conn: sqlite3.Connection,
    a: str,
    b: str,
    *,
    max_depth: int = _PATH_MAX_DEPTH,
) -> dict[str, Any]:
    """Shortest path A → B over RESOLVED call/import edges (BFS).

    Unresolved edges are excluded by design: a shortest path through
    a name-match is invented provenance (design brief §Confidence).
    Ambiguous or unknown endpoints return candidates / empty
    candidate lists instead of guessing.
    """
    max_depth = min(max_depth, _PATH_MAX_DEPTH)
    result: dict[str, Any] = {
        "success": True, "found": False, "hops": [],
        "truncated": False, "max_depth": max_depth,
    }

    a_cands = _symbol_candidates(conn, a)
    b_cands = _symbol_candidates(conn, b)
    if len(a_cands) != 1:
        result["candidates_a"] = a_cands
        return result
    if len(b_cands) != 1:
        result["candidates_b"] = b_cands
        return result

    start_id, goal_id = a_cands[0]["id"], b_cands[0]["id"]
    if start_id == goal_id:
        result["found"] = True
        return result

    # BFS over resolved outbound edges; parent map for path replay.
    visited: dict[str, tuple[str, dict] | None] = {start_id: None}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            result["truncated"] = True
            continue
        edges = conn.execute(
            """SELECT target_symbol_id, kind, confidence, line
               FROM code_relations
               WHERE source_symbol_id = ? AND target_symbol_id IS NOT NULL""",
            (current,),
        ).fetchall()
        for edge in edges:
            nxt = edge["target_symbol_id"]
            if nxt in visited:
                continue
            visited[nxt] = (current, dict(edge))
            if nxt == goal_id:
                queue.clear()
                break
            if len(visited) >= _PATH_MAX_VISITED:
                result["truncated"] = True
                queue.clear()
                break
            queue.append((nxt, depth + 1))

    if goal_id not in visited:
        return result

    # Replay parent chain goal → start, then reverse.
    chain: list[tuple[str, dict]] = []
    node = goal_id
    while visited[node] is not None:
        prev, edge = visited[node]
        chain.append((node, edge))
        node = prev
    chain.reverse()

    hops = []
    for target_id, edge in chain:
        src_id = visited[target_id][0]
        src_q = conn.execute(
            "SELECT qualified_name FROM code_symbols WHERE id = ?",
            (src_id,),
        ).fetchone()["qualified_name"]
        tgt_row = conn.execute(
            "SELECT qualified_name FROM code_symbols WHERE id = ?",
            (target_id,),
        ).fetchone()
        file_path, line = _rel_path_for(conn, src_id)
        hops.append({
            "source_qname": src_q,
            "kind": edge["kind"],
            "confidence": edge["confidence"],
            "target_qname": tgt_row["qualified_name"],
            "file": file_path,
            "line": edge["line"] or line,
        })

    result["found"] = True
    result["hops"] = hops
    return result


def affected(
    conn: sqlite3.Connection,
    x: str,
    *,
    depth: int = 2,
    resolved_only: bool = False,
) -> dict[str, Any]:
    """Reverse traversal: what depends on X, to `depth` hops.

    Grouped by relation kind; each entry carries file:line and a
    confidence label — ``resolved`` edges are facts, ``unresolved``
    are name-matches (joined via target_name ↔ symbols.name).
    Bounded + cycle-safe; truncation is signalled honestly.
    """
    depth = min(depth, _AFFECTED_MAX_DEPTH)
    result: dict[str, Any] = {
        "success": True, "symbol": x, "depth": depth,
        "by_kind": {}, "truncated": False, "resolved_only": resolved_only,
    }

    cands = _symbol_candidates(conn, x)
    if len(cands) != 1:
        result["candidates"] = cands
        return result
    target = cands[0]

    visited: set[str] = {target["id"]}
    frontier: list[tuple[str, int]] = [(target["id"], 1)]
    total = 0

    while frontier:
        current_id, d = frontier.pop(0)
        if d > depth:
            result["truncated"] = True
            continue

        # Inbound resolved edges.
        resolved_rows = conn.execute(
            """SELECT cr.source_symbol_id, cr.kind, cr.confidence,
                      cr.line, cs.qualified_name
               FROM code_relations cr
               JOIN code_symbols cs ON cs.id = cr.source_symbol_id
               WHERE cr.target_symbol_id = ?""",
            (current_id,),
        ).fetchall()
        entries = [dict(r) | {"confidence": "resolved"} for r in resolved_rows]

        # Inbound unresolved edges (name-match): rows whose
        # target_name equals the CURRENT symbol's name.
        if not resolved_only:
            name_row = conn.execute(
                "SELECT name FROM code_symbols WHERE id = ?",
                (current_id,),
            ).fetchone()
            if name_row is not None:
                unresolved_rows = conn.execute(
                    """SELECT cr.source_symbol_id, cr.kind,
                              cr.confidence, cr.line, cs.qualified_name
                       FROM code_relations cr
                       JOIN code_symbols cs
                         ON cs.id = cr.source_symbol_id
                       WHERE cr.target_symbol_id IS NULL
                         AND cr.target_name = ?""",
                    (name_row["name"],),
                ).fetchall()
                entries.extend(
                    dict(r) | {"confidence": "unresolved"}
                    for r in unresolved_rows
                )

        for e in entries:
            if total >= _AFFECTED_MAX_RESULTS:
                result["truncated"] = True
                break
            file_path, line = _rel_path_for(conn, e["source_symbol_id"])
            result["by_kind"].setdefault(e["kind"], []).append({
                "source_qname": e["qualified_name"],
                "kind": e["kind"],
                "file": file_path,
                "line": e["line"] or line,
                "confidence": e["confidence"],
                "depth": d,
            })
            total += 1
            if (
                e["confidence"] == "resolved"
                and e["source_symbol_id"] not in visited
            ):
                visited.add(e["source_symbol_id"])
                frontier.append((e["source_symbol_id"], d + 1))

    return result


def hubs(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    resolved_only: bool = False,
) -> dict[str, Any]:
    """Most-connected symbols (architectural hot spots).

    Degree split: out = edges where the symbol is source; in =
    resolved inbound + (unless resolved_only) unresolved name-match
    inbound (target_name = symbol name).
    """
    limit = max(1, min(limit, 200))
    unresolved_clause = "" if resolved_only else """
        + (SELECT COUNT(*) FROM code_relations cr
           WHERE cr.target_symbol_id IS NULL
             AND cr.target_name = cs.name)"""
    resolved_filter = (
        "AND cr2.target_symbol_id IS NOT NULL" if resolved_only else ""
    )
    rows = conn.execute(
        f"""SELECT cs.qualified_name, cs.kind, cs.line_start,
                   cb.file_path,
              (SELECT COUNT(*) FROM code_relations cr
               WHERE cr.source_symbol_id = cs.id
               {resolved_filter.replace('cr2', 'cr')}) AS out_degree,
              ((SELECT COUNT(*) FROM code_relations cr2
                WHERE cr2.target_symbol_id = cs.id)
               {unresolved_clause}) AS in_degree
           FROM code_symbols cs
           JOIN codebase_scans cb ON cb.file_memory_id = cs.file_memory_id
           WHERE cs.kind != 'MODULE'
           ORDER BY (out_degree + in_degree) DESC, cs.qualified_name
           LIMIT ?""",
        (limit,),
    ).fetchall()

    return {
        "success": True,
        "resolved_only": resolved_only,
        "hubs": [
            {
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file": r["file_path"],
                "line": r["line_start"],
                "in_degree": r["in_degree"],
                "out_degree": r["out_degree"],
                "degree": r["in_degree"] + r["out_degree"],
            }
            for r in rows
        ],
    }
