"""M5 T3 — `sage-memory dedup` CLI subcommand.

Three modes per spec A7-A9:
  Default (worker path)   Enqueue a `dedup` task; print task id; exit.
                          LLM-key required (else clear error message).
                          At-most-one pending+running dedup task —
                          duplicate enqueues are dropped silently
                          (returns existing task id).
  --sync                  Run dedup in-process; per-pair stdout logs;
                          uses sqlite advisory lock (PRAGMA
                          application_id + BEGIN IMMEDIATE) to
                          serialize against a running worker task.
  --provider stub         No LLM call; produces cost-estimation report.
                          Argparse rejects --provider stub without
                          --sync (prevents silently-no-op enqueued
                          tasks).

LLM-key gate: with no ANTHROPIC_API_KEY / OPENAI_API_KEY, default
mode + `--sync` mode exit nonzero with a clear error message naming
the env vars. `--provider stub` is independent of LLM-key state.
"""

from __future__ import annotations

import logging
import sys
import uuid

from . import dedup as _dedup
from . import llm as _llm
from .db import get_project_db


logger = logging.getLogger("sage_memory.cli_dedup")


# Sage application_id — 'SAGM' in ASCII = 0x5341474D. Diagnostic
# identification for the BEGIN IMMEDIATE advisory lock.
_SAGE_APPLICATION_ID = 0x5341474D


_HELP_TEXT = """\
sage-memory dedup — entity deduplication (LLM-confirmed)

Usage:
  sage-memory dedup
      Default: enqueue a `dedup` task for the background worker.
      Returns the task id; worker processes at next poll cycle.
      Requires ANTHROPIC_API_KEY or OPENAI_API_KEY.

  sage-memory dedup --sync
      Run dedup synchronously in-process. Per-pair decisions are
      printed to stdout. Blocks until completion. Returns nonzero
      exit on LLM failure.

  sage-memory dedup --sync --provider stub
      Cosine pre-filter only; no LLM call. Produces a cost-estimation
      report (candidate-pair count × $0.0002 per pair). LLM key is
      NOT required.

Algorithm (per ADR-003):
  Group entities by type; for pairs with cosine > 0.9 on the name
  embedding, ask the LLM to confirm; on yes, set canonical_id of
  one to the other.
"""


def run_dedup(argv: list[str]) -> int:
    """Entry point dispatched from `__init__.py:main()`. Returns exit code."""
    if argv and argv[0] in ("-h", "--help"):
        print(_HELP_TEXT)
        return 0

    flags = _parse_flags(argv)
    if flags is None:
        return 2

    if flags.provider_stub and not flags.sync:
        print(
            "sage-memory dedup: --provider stub requires --sync "
            "(stub-mode async enqueue would silently no-op)\n",
            file=sys.stderr,
        )
        print(_HELP_TEXT, file=sys.stderr)
        return 2

    db = get_project_db()
    if db is None:
        print(
            "sage-memory dedup: no project DB found\n", file=sys.stderr,
        )
        return 2

    # Stub mode: always allowed, independent of LLM-key state.
    if flags.provider_stub:
        return _run_stub(db)

    # LLM-key gate for default + --sync.
    if not _llm.is_configured():
        print(
            "sage-memory dedup: no LLM key configured "
            "(set ANTHROPIC_API_KEY or OPENAI_API_KEY)\n",
            file=sys.stderr,
        )
        return 1

    if flags.sync:
        return _run_sync(db)
    return _run_async_enqueue(db)


# ─── Flag parsing ─────────────────────────────────────────────────


class _Flags:
    sync: bool = False
    provider_stub: bool = False


def _parse_flags(argv: list[str]) -> _Flags | None:
    flags = _Flags()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sync":
            flags.sync = True
            i += 1
        elif a == "--provider":
            if i + 1 >= len(argv):
                print(
                    "sage-memory dedup: --provider requires a value\n",
                    file=sys.stderr,
                )
                print(_HELP_TEXT, file=sys.stderr)
                return None
            if argv[i + 1] != "stub":
                print(
                    f"sage-memory dedup: --provider must be 'stub' "
                    f"(got {argv[i + 1]!r})\n",
                    file=sys.stderr,
                )
                return None
            flags.provider_stub = True
            i += 2
        else:
            print(
                f"sage-memory dedup: unknown flag: {a}\n",
                file=sys.stderr,
            )
            print(_HELP_TEXT, file=sys.stderr)
            return None
    return flags


# ─── Mode implementations ─────────────────────────────────────────


def _run_async_enqueue(db) -> int:
    """Default mode: enqueue a dedup task for the worker. At-most-one
    contract: if a pending+running dedup task already exists, return
    its id without inserting a new row."""
    existing = db.execute(
        "SELECT id FROM extraction_queue "
        "WHERE task_type = 'dedup' AND status IN ('pending', 'running') "
        "LIMIT 1"
    ).fetchone()
    if existing is not None:
        print(
            f"sage-memory dedup: task {existing['id']} already pending. "
            f"Run `sage-memory worker --status` to monitor."
        )
        return 0

    task_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO extraction_queue "
        "(id, memory_id, task_type, status, attempts, created_at) "
        "VALUES (?, NULL, 'dedup', 'pending', 0, unixepoch())",
        (task_id,),
    )
    db.commit()
    print(
        f"sage-memory dedup: enqueued task {task_id}. "
        f"Run `sage-memory worker --status` to monitor."
    )
    return 0


def _run_sync(db) -> int:
    """`--sync` mode: in-process dedup with sqlite advisory lock.

    `PRAGMA application_id` is set for diagnostic identification.
    `BEGIN IMMEDIATE` takes the actual write-lock that blocks any
    concurrent writer (including the worker's dedup task processor)
    until COMMIT/ROLLBACK.
    """
    db.execute(f"PRAGMA application_id = {_SAGE_APPLICATION_ID}")
    db.execute("BEGIN IMMEDIATE")
    try:
        summary = _dedup.run_pass(
            db, llm_confirm=True, log_decisions=True,
        )
        db.execute("COMMIT")
    except Exception as e:
        db.execute("ROLLBACK")
        print(
            f"sage-memory dedup: LLM call failed ({type(e).__name__}: {e})",
            file=sys.stderr,
        )
        return 1

    print(
        f"sage-memory dedup --sync: considered "
        f"{summary['pairs_considered']} pair(s), confirmed "
        f"{summary['pairs_confirmed']}, merged "
        f"{summary['pairs_merged']}. "
        f"Est cost: ${summary['cost_estimate_usd']:.4f}"
    )
    return 0


def _run_stub(db) -> int:
    """`--provider stub` mode: cosine pre-filter only, no LLM."""
    summary = _dedup.run_pass(db, llm_confirm=False, log_decisions=False)
    print(
        f"sage-memory dedup --provider stub: "
        f"{summary['pairs_considered']} candidate pair(s). "
        f"Est LLM cost if confirmed: "
        f"${summary['cost_estimate_usd']:.4f}"
    )
    return 0
