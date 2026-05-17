"""M5 T4 — `sage-memory queue prune` manual CLI subcommand.

Manual operator-initiated entry point for extraction_queue retention.
Runs the same prune query as the worker's `maybe_prune()` BUT
bypasses the 24h gate (operator-initiated = always runs). Updates
worker_state.last_prune_at on success.

Per ADR-001 + ADR-003 §Retention: 30-day cutoff on `done`/`failed`
rows. Idempotent with the worker's auto-prune.
"""

from __future__ import annotations

import sys
import time

from .db import get_project_db


_RETENTION_SECONDS = 30 * 86400


_HELP_TEXT = """\
sage-memory queue — extraction_queue maintenance commands

Usage:
  sage-memory queue prune
      Delete `done` and `failed` extraction_queue rows older than
      30 days. Updates worker_state.last_prune_at. Idempotent with
      the worker's auto-prune (which runs once per 24h); the manual
      command ALWAYS runs regardless of when auto-prune last ran.

  sage-memory queue --help
      Show this help.
"""


def run_queue(argv: list[str]) -> int:
    """Entry point dispatched from `__init__.py:main()`."""
    if not argv or argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    if argv[0] != "prune":
        print(
            f"sage-memory queue: unknown subcommand: {argv[0]}\n",
            file=sys.stderr,
        )
        print(_HELP_TEXT, file=sys.stderr)
        return 2

    db = get_project_db()
    if db is None:
        print(
            "sage-memory queue prune: no project DB found\n",
            file=sys.stderr,
        )
        return 2

    cutoff = time.time() - _RETENTION_SECONDS
    cur = db.execute(
        "DELETE FROM extraction_queue "
        "WHERE status IN ('done', 'failed') "
        "  AND processed_at < ?",
        (cutoff,),
    )
    db.execute(
        "UPDATE worker_state SET last_prune_at = ? WHERE id = 1",
        (time.time(),),
    )
    db.commit()
    print(
        f"sage-memory queue prune: deleted {cur.rowcount} aged row(s)."
    )
    return 0
