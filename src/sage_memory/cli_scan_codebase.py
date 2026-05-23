"""``sage-memory scan-codebase`` CLI subcommand.

Parses the flags documented in spec §"CLI: ``sage-memory
scan-codebase``" and prints the summary block to stdout. Exit codes
follow spec line 191:

  0 — Scan completed (including no-op when all hashes unchanged).
      Per-file ERROR nodes are NOT a failure.
  1 — Unrecognized flag, path doesn't exist, or refusing to scan a
      special location (home-directory guard added in T10b).
  2 — ``[codebase]`` extra not installed (install-hint to stderr).
  3 — ``--limit`` exceeded (T10b enforces; T10a only parses the
      value).
  4 — Catastrophic per-file failures occurred during scan.

The summary text follows spec §"Summary output" exactly — the
``parse:`` line is conditional on ``files_with_error_nodes > 0``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__ as _version


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sage-memory scan-codebase",
        description=(
            "Scan local source code with tree-sitter; populate the "
            "project's code-symbol index. Requires the [codebase] "
            "pip extra."
        ),
        exit_on_error=False,
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help=(
            "Directory to scan. Defaults to the active project root "
            "(walks up from cwd looking for .git / pyproject.toml / "
            "package.json / Cargo.toml / go.mod; falls back to cwd)."
        ),
    )
    p.add_argument(
        "--languages",
        type=str,
        default=None,
        help=(
            "Comma-separated subset of "
            "{py,ts,js,go,rs,java,rb,php,c,cpp}. Default: all detected "
            "by file extension."
        ),
    )
    p.add_argument(
        "--include-ignored",
        action="store_true",
        help="Skip .gitignore filtering; scan everything.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=5000,
        help=(
            "Max number of files to scan. Default: 5000. Exits with "
            "code 3 when exceeded (T10b enforces)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + count + report; write nothing.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-parse all files even if content-hash is unchanged.",
    )
    return p


def run_scan_codebase(argv: Sequence[str]) -> int:
    """Entry point — returns int exit code per spec §"Exit codes"."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        # argparse exits 0 on --help, 2 on error. Preserve both:
        # spec line 192 maps --help to exit 0, bad flag to exit 1
        # (NOT argparse's default 2).
        if exc.code in (0, None):
            return 0
        return 1
    except argparse.ArgumentError:
        return 1

    languages: list[str] | None = None
    if args.languages:
        languages = [s.strip() for s in args.languages.split(",") if s.strip()]

    # Lazy-import to keep --help / argparse fast and to surface the
    # missing-extra error with a clean install hint.
    try:
        from .codebase import scan
    except ImportError as exc:
        print(_INSTALL_HINT, file=sys.stderr)
        return 2

    # Path validation BEFORE invoking scan() so we can return exit 1
    # for missing paths without depending on scan() internals.
    if args.path is not None:
        p = Path(args.path)
        if not p.exists():
            print(
                f"sage-memory scan-codebase: path does not exist: {args.path}",
                file=sys.stderr,
            )
            return 1
        if not p.is_dir():
            print(
                f"sage-memory scan-codebase: path is not a directory: {args.path}",
                file=sys.stderr,
            )
            return 1

    # Lazy-import the exception classes so the module can be imported
    # without the [codebase] extra (only their `isinstance` checks need
    # the import — they're resolved at call time, not module load).
    from .codebase import (
        ScanLimitExceeded, ScanLockHeld, ScanRefused,
    )

    try:
        result = scan(
            root=args.path,
            languages=languages,
            include_ignored=args.include_ignored,
            limit=args.limit,
            force=args.force,
            dry_run=args.dry_run,
        )
    except ScanLimitExceeded as exc:
        # Spec line 191: exit 3 when --limit exceeded.
        print(f"sage-memory scan-codebase: {exc}", file=sys.stderr)
        return 3
    except ScanLockHeld as exc:
        # Concurrent scan running on this project → exit 1 with a
        # human-readable hint per rev 3 Minor #5 stderr text.
        print(
            f"sage-memory scan-codebase: scan already in progress "
            f"({exc})",
            file=sys.stderr,
        )
        return 1
    except ScanRefused as exc:
        # Home-dir or other refused-to-scan case → exit 1.
        print(f"sage-memory scan-codebase: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # _require_extra raises RuntimeError when the [codebase]
        # extra isn't installed — propagates as exit 2 with the
        # install hint already in the message.
        msg = str(exc)
        if "requires the [codebase] extra" in msg:
            print(msg, file=sys.stderr)
            return 2
        # Any other RuntimeError (e.g. project-root resolution)
        # → exit 1.
        print(f"sage-memory scan-codebase: {msg}", file=sys.stderr)
        return 1

    print(_format_summary(result))
    return 4 if result.parse_errors > 0 else 0


def _format_summary(result) -> str:
    """Render the spec §"Summary output" block. The ``parse:`` line is
    OMITTED when ``files_with_error_nodes == 0`` (spec line 229).
    """
    lines = [f"sage-memory scan-codebase v{_version}"]
    lines.append(f"  project root: {result.project_root}")
    langs = ", ".join(result.languages_detected) or "(none detected)"
    lines.append(f"  languages: {langs}")

    if result.dry_run:
        lines.append(
            f"  dry-run: {result.files_scanned} files would be scanned"
        )
        lines.append(f"  elapsed: {result.elapsed_ms / 1000:.1f}s")
        return "\n".join(lines)

    lines.append(
        f"  files: {result.files_scanned} scanned, "
        f"{result.files_changed} changed, "
        f"{result.files_unchanged} unchanged "
        f"(skipped via content-hash)"
    )
    if result.files_with_error_nodes > 0:
        lines.append(
            f"  parse: {result.files_with_error_nodes} files had "
            f"tree-sitter ERROR nodes (well-formed parts extracted)"
        )

    sym_parts = []
    pretty = {
        "FUNCTION": "functions",
        "CLASS": "classes",
        "METHOD": "methods",
        "INTERFACE": "interfaces",
        "STRUCT": "structs",
        "ENUM": "enums",
        "CONST": "constants",
    }
    for kind in ("FUNCTION", "CLASS", "METHOD", "INTERFACE", "STRUCT",
                 "ENUM", "CONST"):
        n = result.symbols_by_kind.get(kind, 0)
        if n > 0:
            sym_parts.append(f"{n:,} {pretty[kind]}")
    if sym_parts:
        lines.append("  symbols: " + ", ".join(sym_parts))

    lines.append(
        f"  relations: {result.relations_imports:,} imports, "
        f"{result.relations_calls_resolved + result.relations_calls_unresolved:,} "
        f"calls ({result.relations_calls_resolved:,} resolved, "
        f"{result.relations_calls_unresolved:,} unresolved)"
    )
    lines.append(f"  elapsed: {result.elapsed_ms / 1000:.1f}s")
    return "\n".join(lines)


_INSTALL_HINT = (
    "scan-codebase requires the [codebase] extra:\n"
    "  pip install 'sage-memory[codebase]'\n"
    "or with uvx, set args to ['sage-memory[codebase]']"
)
