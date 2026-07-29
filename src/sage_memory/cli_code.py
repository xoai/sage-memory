"""P2-1 — `sage-memory code` CLI subcommand tree (SM-CAP-01).

Structural code-graph queries over the scanned code graph. Hand-rolled
flag parsing matching the cli_dedup/cli_hub pattern. Human-readable
text by default; ``--json`` for machine consumption. Semantics per
docs/design/code-graph-queries.md.
"""

from __future__ import annotations

import json
import sys


_HELP_TEXT = """\
sage-memory code — structural code-graph queries (requires [codebase] extra
and a prior `sage-memory scan-codebase` run)

Usage:
  sage-memory code path <A> <B> [--max-depth N] [--json]
  sage-memory code affected <X> [--depth N] [--resolved-only] [--json]
  sage-memory code hubs [--limit K] [--resolved-only] [--json]

Commands:
  path      Shortest call/import path between two symbols over
            RESOLVED edges only (unresolved name-matches are never
            hops — a path through a guess would be false provenance).
  affected  What depends on a symbol (reverse traversal, grouped by
            relation kind). Unresolved edges are labelled
            "unresolved" (name-match); --resolved-only drops them.
  hubs      Most-connected symbols with in/out degree split.
"""


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _render_path(result: dict) -> None:
    if result.get("candidates_a") or result.get("candidates_b"):
        for side in ("candidates_a", "candidates_b"):
            cands = result.get(side)
            if cands:
                name = "A" if side == "candidates_a" else "B"
                print(f"ambiguous {name} — {len(cands)} candidates:")
                for c in cands:
                    print(f"  {c['qualified_name']} ({c['kind']}) "
                          f"{c['file']}:{c['line']}")
        if not (result.get("candidates_a") and result["candidates_a"]):
            pass
        return
    if not result["found"]:
        print("no resolved path found "
              f"(max_depth={result['max_depth']})")
        return
    hops = result["hops"]
    if not hops:
        print("A and B are the same symbol")
        return
    print(f"{hops[0]['source_qname']}()")
    for h in hops:
        print(f"  --{h['kind']}[{h['confidence']}]--> "
              f"{h['target_qname']}()   ({h['file']}:{h['line']})")
    if result["truncated"]:
        print(f"... truncated at max_depth={result['max_depth']}")


def _render_affected(result: dict) -> None:
    if result.get("candidates"):
        print(f"ambiguous symbol — {len(result['candidates'])} candidates:")
        for c in result["candidates"]:
            print(f"  {c['qualified_name']} ({c['kind']}) "
                  f"{c['file']}:{c['line']}")
        return
    total = 0
    for kind, entries in sorted(result["by_kind"].items()):
        print(f"{kind}:")
        for e in entries:
            label = "" if e["confidence"] == "resolved" else " [name-match]"
            print(f"  {e['source_qname']}   {e['file']}:{e['line']}"
                  f"{label} (depth {e['depth']})")
            total += 1
    if total == 0:
        print(f"nothing depends on {result['symbol']} "
              f"(depth={result['depth']})")
    if result["truncated"]:
        print("... truncated (result cap reached)")


def _render_hubs(result: dict) -> None:
    print(f"{'degree':>6} {'in':>5} {'out':>5}  symbol")
    for h in result["hubs"]:
        print(f"{h['degree']:>6} {h['in_degree']:>5} {h['out_degree']:>5}  "
              f"{h['qualified_name']} ({h['kind']}) {h['file']}:{h['line']}")


def run_code(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    cmd, rest = argv[0], argv[1:]
    json_out = "--json" in rest
    resolved_only = "--resolved-only" in rest
    positional = [a for a in rest if not a.startswith("--")]

    def _int_flag(name: str, default: int) -> int:
        if name in rest:
            i = rest.index(name)
            if i + 1 < len(rest):
                try:
                    return int(rest[i + 1])
                except ValueError:
                    return default
        return default

    try:
        from .codebase.queries import affected, find_path, hubs
    except ImportError:
        print(
            "sage-memory code requires the [codebase] extra. "
            "Install with: pip install 'sage-memory[codebase]'",
            file=sys.stderr,
        )
        return 2

    from .db import get_project_db
    conn = get_project_db()
    if conn is None:
        print(
            "sage-memory code: no active project (run from inside a "
            "project or call sage_memory_set_project)",
            file=sys.stderr,
        )
        return 1

    if cmd == "path":
        if len(positional) < 2:
            print(_HELP_TEXT, file=sys.stderr)
            return 1
        result = find_path(
            conn, positional[0], positional[1],
            max_depth=_int_flag("--max-depth", 16),
        )
        _print_json(result) if json_out else _render_path(result)
        return 0 if result["found"] else 1

    if cmd == "affected":
        if not positional:
            print(_HELP_TEXT, file=sys.stderr)
            return 1
        result = affected(
            conn, positional[0],
            depth=_int_flag("--depth", 2),
            resolved_only=resolved_only,
        )
        _print_json(result) if json_out else _render_affected(result)
        return 0

    if cmd == "hubs":
        result = hubs(
            conn,
            limit=_int_flag("--limit", 20),
            resolved_only=resolved_only,
        )
        _print_json(result) if json_out else _render_hubs(result)
        return 0

    print(f"sage-memory code: unknown subcommand: {cmd}\n",
          file=sys.stderr)
    print(_HELP_TEXT, file=sys.stderr)
    return 1
