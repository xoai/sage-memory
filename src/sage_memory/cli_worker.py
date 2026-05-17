"""Implementation of `sage-memory worker --status` subcommand.

Prints queue depth by status + task_type. Reads the DB directly via
its own connection — does NOT talk to a running worker (the CLI is
a separate process).
"""

from __future__ import annotations

from .db import get_db


def print_worker_status() -> None:
    db = get_db()

    rows = db.execute(
        "SELECT status, task_type, COUNT(*) AS n "
        "FROM extraction_queue "
        "GROUP BY status, task_type "
        "ORDER BY status, task_type"
    ).fetchall()

    # Aggregate by status
    by_status: dict[str, dict[str, int]] = {}
    for r in rows:
        by_status.setdefault(r["status"], {})[r["task_type"]] = r["n"]

    print("sage-memory worker queue")
    print("─" * 60)
    print("  Queue depth:")
    for status in ("pending", "running", "done", "failed"):
        types = by_status.get(status, {})
        total = sum(types.values()) if types else 0
        breakdown_parts = [
            f"{tt}: {n}"
            for tt, n in sorted(types.items())
        ]
        breakdown = (
            "  (" + ", ".join(breakdown_parts) + ")"
            if breakdown_parts else ""
        )
        print(f"    {status:8}{total}{breakdown}")
