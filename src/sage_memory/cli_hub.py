"""M2.2 — `sage-memory hub` CLI subcommand tree.

Per ADR-008 §"Decision" + plan M2.2 done-when. Five subcommands ship
in this milestone: ``init``, ``add``, ``remove``, ``list``, ``status``.
``search`` lands in M2.4; ``store`` / ``import`` / ``release`` in M3.

Flag parsing follows the cli_dedup hand-rolled pattern (no argparse)
so the dispatch shape stays consistent with the rest of sage-memory's
CLI. A ``--config-path <path>`` flag overrides the default
``~/.sage-hub.yaml`` location; primarily used by tests, but also a
documented user knob for non-standard hub layouts.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import hub
from .hub import config as hub_config


logger = logging.getLogger("sage_memory.cli_hub")


# Valid hub project names: alphanumeric + dash + underscore, 1-64 chars.
# Slashes/spaces/dots break the YAML lookup path UX and cause confusion
# at the CLI ("hub remove foo/bar" would look like a path).
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


_HELP_TEXT = """\
sage-memory hub — cross-project federation (0.13.0+)

Usage:
  sage-memory hub init                       Create empty ~/.sage-hub.yaml
  sage-memory hub add <path> [flags]         Register a project
  sage-memory hub remove <name>              Unregister a project
  sage-memory hub list                       Show registered projects
  sage-memory hub status                     Per-project DB stats
  sage-memory hub search <query> [flags]     Cross-project fan-out search
  sage-memory hub store --to <name> ...      Routed-write to a writable project
  sage-memory hub import --from <path> ...   Copy memories from a source DB
  sage-memory hub release <name>             Voluntarily release ownership

Common flags:
  --config-path <path>   Override ~/.sage-hub.yaml location.

`add` flags:
  --name <slug>          Explicit project name (default: basename of path).
  --searchable           Include in `hub search` fan-out (default).
  --no-searchable        Exclude from `hub search` fan-out.
  --writable             Accept `hub store --to <name>` (default: off).
  --no-writable          Reject `hub store --to <name>` (the default).
"""


def run_hub(argv: list[str]) -> int:
    """Entry point dispatched from ``__init__.py:main()``. Returns exit code."""
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0 if argv else 2

    sub = argv[0]
    rest = argv[1:]

    dispatch = {
        "init": _run_init,
        "add": _run_add,
        "remove": _run_remove,
        "list": _run_list,
        "status": _run_status,
        "search": _run_search,
        "store": _run_store,
        "import": _run_import,
        "release": _run_release,
    }
    handler = dispatch.get(sub)
    if handler is None:
        print(
            f"sage-memory hub: unknown subcommand: {sub}\n",
            file=sys.stderr,
        )
        print(_HELP_TEXT, file=sys.stderr)
        return 2
    return handler(rest)


# ─── Shared flag plumbing ─────────────────────────────────────────


def _take_config_path(argv: list[str]) -> tuple[list[str], Path]:
    """Pull ``--config-path <path>`` out of argv if present, returning
    the residual argv and the resolved hub config path."""
    out: list[str] = []
    i = 0
    explicit: Path | None = None
    while i < len(argv):
        if argv[i] == "--config-path":
            if i + 1 >= len(argv):
                raise _FlagError("--config-path requires a value")
            explicit = Path(argv[i + 1])
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out, explicit or hub_config.DEFAULT_HUB_PATH


class _FlagError(ValueError):
    """Raised by flag parsing to signal exit-2 with a hand-rolled error."""


def _print_error(message: str) -> None:
    print(f"sage-memory hub: {message}\n", file=sys.stderr)
    print(_HELP_TEXT, file=sys.stderr)


# ─── init ─────────────────────────────────────────────────────────


def _run_init(argv: list[str]) -> int:
    try:
        residual, path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if residual:
        _print_error(f"`init` takes no positional args: {residual}")
        return 2
    if path.exists():
        print(
            f"sage-memory hub: {path} already exists; nothing to do.",
        )
        return 0
    hub_config.init(path)
    print(f"sage-memory hub: created {path}")
    return 0


# ─── add ──────────────────────────────────────────────────────────


def _run_add(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2

    # Pre-parse named flags so we can validate before consuming positional.
    name: str | None = None
    searchable: bool = True
    writable: bool = False
    positional: list[str] = []

    i = 0
    while i < len(residual):
        a = residual[i]
        if a == "--name":
            if i + 1 >= len(residual):
                _print_error("--name requires a value")
                return 2
            name = residual[i + 1]
            i += 2
        elif a == "--searchable":
            searchable = True
            i += 1
        elif a == "--no-searchable":
            searchable = False
            i += 1
        elif a == "--writable":
            writable = True
            i += 1
        elif a == "--no-writable":
            writable = False
            i += 1
        elif a.startswith("-"):
            _print_error(f"unknown flag: {a}")
            return 2
        else:
            positional.append(a)
            i += 1

    if not positional:
        _print_error("`add` requires a project path positional arg")
        return 2
    if len(positional) > 1:
        _print_error(f"`add` takes one positional path; got {positional}")
        return 2

    project_path = Path(positional[0]).resolve()
    if not project_path.exists() or not project_path.is_dir():
        _print_error(
            f"project path does not exist or is not a directory: "
            f"{project_path}"
        )
        return 2

    final_name = name or project_path.name
    if not _NAME_PATTERN.match(final_name):
        _print_error(
            f"project name {final_name!r} must match "
            f"[A-Za-z0-9_-]{{1,64}}"
        )
        return 2

    if not hub_path.exists():
        _print_error(
            f"hub config not found at {hub_path}. "
            f"Run `sage-memory hub init` first."
        )
        return 2

    cfg = hub_config.load(hub_path)
    try:
        cfg = hub_config.add_project(
            cfg,
            final_name,
            project_path,
            searchable=searchable,
            writable=writable,
        )
    except ValueError as e:
        _print_error(str(e))
        return 2
    hub_config.save(cfg, hub_path)
    print(
        f"sage-memory hub: added {final_name!r} "
        f"(searchable={searchable}, writable={writable})"
    )
    return 0


# ─── remove ───────────────────────────────────────────────────────


def _run_remove(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if not residual:
        _print_error("`remove` requires a project name positional arg")
        return 2
    if len(residual) > 1:
        _print_error(f"`remove` takes one positional name; got {residual}")
        return 2

    name = residual[0]
    if not hub_path.exists():
        _print_error(
            f"hub config not found at {hub_path}. "
            f"Run `sage-memory hub init` first."
        )
        return 2

    cfg = hub_config.load(hub_path)
    try:
        cfg = hub_config.remove_project(cfg, name)
    except KeyError as e:
        _print_error(str(e))
        return 2
    hub_config.save(cfg, hub_path)
    print(f"sage-memory hub: removed {name!r}")
    return 0


# ─── list ─────────────────────────────────────────────────────────


def _run_list(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if residual:
        _print_error(f"`list` takes no positional args: {residual}")
        return 2

    if not hub_path.exists():
        print(
            f"sage-memory hub: no config at {hub_path}. "
            f"Run `sage-memory hub init` to create one."
        )
        return 0

    cfg = hub_config.load(hub_path)
    if not cfg.projects:
        print("sage-memory hub: no projects registered.")
        return 0

    print(f"{'NAME':<24} {'SEARCHABLE':<11} {'WRITABLE':<9} PATH")
    for p in cfg.projects:
        print(
            f"{p.name:<24} {str(p.searchable):<11} {str(p.writable):<9} {p.path}"
        )
    return 0


# ─── status ───────────────────────────────────────────────────────


def _run_status(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if residual:
        _print_error(f"`status` takes no positional args: {residual}")
        return 2

    if not hub_path.exists():
        print(
            f"sage-memory hub: no config at {hub_path}. "
            f"Run `sage-memory hub init` to create one."
        )
        return 0

    cfg = hub_config.load(hub_path)
    if not cfg.projects:
        print("sage-memory hub: no projects registered.")
        return 0

    print(f"{'NAME':<24} {'MEMORIES':<10} {'SIZE':<10} MTIME")
    for p in cfg.projects:
        db = p.path / ".sage-memory" / "memory.db"
        if not db.exists():
            print(f"{p.name:<24} {'(no db)':<10} {'-':<10} -")
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            count_row = conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()
            count = count_row[0] if count_row else 0
            conn.close()
        except sqlite3.DatabaseError as exc:
            print(f"{p.name:<24} {'(error)':<10} -          {exc}")
            continue
        size_kb = db.stat().st_size // 1024
        mtime = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(db.stat().st_mtime),
        )
        print(f"{p.name:<24} {count:<10} {size_kb}K{'':<6} {mtime}")
    return 0


# ─── search ───────────────────────────────────────────────────────


def _run_search(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2

    limit: int = 5
    tags: list[str] = []
    positional: list[str] = []

    i = 0
    while i < len(residual):
        a = residual[i]
        if a == "--limit":
            if i + 1 >= len(residual):
                _print_error("--limit requires a value")
                return 2
            try:
                limit = int(residual[i + 1])
            except ValueError:
                _print_error(f"--limit must be an integer (got {residual[i + 1]!r})")
                return 2
            i += 2
        elif a == "--tags":
            # M2 review M2: single-tag-per-flag avoids swallowing the
            # query positional. For multiple tags, repeat the flag.
            if i + 1 >= len(residual):
                _print_error("--tags requires a value")
                return 2
            tags.append(residual[i + 1])
            i += 2
        elif a.startswith("-"):
            _print_error(f"unknown flag: {a}")
            return 2
        else:
            positional.append(a)
            i += 1

    if not positional:
        _print_error("`search` requires a query positional arg")
        return 2
    if len(positional) > 1:
        _print_error(f"`search` takes one positional query; got {positional}")
        return 2

    query = positional[0]
    from .hub import search as hub_search

    try:
        envelope = hub_search.fan_out_search(
            query, hub_path=hub_path, limit=limit, tags=tags or None,
        )
    except ValueError as e:
        _print_error(str(e))
        return 2

    results = envelope.get("results", [])
    if not results:
        print(f"sage-memory hub search: no matches for {query!r}")
        return 0

    print(
        f"sage-memory hub search: {len(results)} result(s) "
        f"across {len(envelope.get('sources', []))} source(s)"
    )
    for r in results:
        title = r.get("title") or "(no title)"
        score = r.get("rrf_score", 0.0)
        source = r.get("source", "?")
        print(f"  [{source}] {title}  (rrf={score:.4f})  id={r.get('id')}")
    return 0


# ─── store ────────────────────────────────────────────────────────


def _run_store(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2

    to_name: str | None = None
    content: str | None = None
    title: str | None = None
    tags: list[str] = []

    i = 0
    while i < len(residual):
        a = residual[i]
        if a == "--to":
            if i + 1 >= len(residual):
                _print_error("--to requires a value")
                return 2
            to_name = residual[i + 1]
            i += 2
        elif a == "--content":
            if i + 1 >= len(residual):
                _print_error("--content requires a value")
                return 2
            content = residual[i + 1]
            i += 2
        elif a == "--title":
            if i + 1 >= len(residual):
                _print_error("--title requires a value")
                return 2
            title = residual[i + 1]
            i += 2
        elif a == "--tags":
            if i + 1 >= len(residual):
                _print_error("--tags requires a value")
                return 2
            tags.append(residual[i + 1])
            i += 2
        elif a.startswith("-"):
            _print_error(f"unknown flag: {a}")
            return 2
        else:
            _print_error(f"`store` takes no positional args: {a}")
            return 2

    if not to_name:
        _print_error("`store` requires --to <name>")
        return 2
    if not content:
        _print_error("`store` requires --content <text>")
        return 2

    from .hub.store import store_to_project
    res = store_to_project(
        to_name,
        content=content,
        title=title,
        tags=tags or None,
        hub_path=hub_path,
    )
    if res.get("success"):
        print(
            f"sage-memory hub: stored memory {res['id']!r} in {to_name!r}"
        )
        return 0
    _print_error(res.get("message") or "store failed")
    return 2


# ─── import ───────────────────────────────────────────────────────


def _run_import(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2

    source: Path | None = None
    to_name: str | None = None

    i = 0
    while i < len(residual):
        a = residual[i]
        if a == "--from":
            if i + 1 >= len(residual):
                _print_error("--from requires a value")
                return 2
            source = Path(residual[i + 1])
            i += 2
        elif a == "--to":
            if i + 1 >= len(residual):
                _print_error("--to requires a value")
                return 2
            to_name = residual[i + 1]
            i += 2
        elif a.startswith("-"):
            _print_error(f"unknown flag: {a}")
            return 2
        else:
            _print_error(f"`import` takes no positional args: {a}")
            return 2

    if source is None:
        _print_error("`import` requires --from <source_db_path>")
        return 2
    if to_name is None:
        # Default per ADR-008: new project named after source basename.
        # For v1 we require an explicit --to (the auto-naming path
        # would also need to call `hub add` first; one knob per
        # ship.)
        _print_error(
            "`import` requires --to <project_name> (auto-naming "
            "the destination is not implemented in v1; pre-register "
            "the target with `hub add`)"
        )
        return 2

    from .hub.importer import import_from_source
    res = import_from_source(source, to_name, hub_path=hub_path)
    if res.get("success"):
        print(f"sage-memory hub: {res.get('message')}")
        return 0
    _print_error(res.get("message") or "import failed")
    return 2


# ─── release ──────────────────────────────────────────────────────


def _run_release(argv: list[str]) -> int:
    try:
        residual, hub_path = _take_config_path(argv)
    except _FlagError as e:
        _print_error(str(e))
        return 2
    if not residual:
        _print_error("`release` requires a project name positional arg")
        return 2
    if len(residual) > 1:
        _print_error(f"`release` takes one positional name; got {residual}")
        return 2

    name = residual[0]
    if not hub_path.exists():
        _print_error(
            f"hub config not found at {hub_path}. "
            f"Run `sage-memory hub init` first."
        )
        return 2

    cfg = hub_config.load(hub_path)
    project = next((p for p in cfg.projects if p.name == name), None)
    if project is None:
        _print_error(f"no hub project named {name!r}")
        return 2

    from .hub import ownership as hub_ownership
    token = hub_ownership.registry.get(str(project.path))
    if token is None:
        # No-op for projects we don't own — matches ADR-009's
        # idempotent-release contract.
        print(
            f"sage-memory hub: project {name!r} is not owned by this "
            f"process; nothing to release."
        )
        return 0

    hub_ownership.release(token)
    print(f"sage-memory hub: released ownership of {name!r}")
    return 0
