"""Background queue worker — M3a (T3).

A single-threaded `threading.Thread` drains `extraction_queue`.
Three task types: `extract` (LLM call + entity/mention/relation writes),
`reembed` (per-memory embed via T6a's `memory_id`-filtered
`embed_pending` / `embed_pending_chunks`), `dedup` (stub for M5).

Worker uses its own per-thread sqlite3 connection — opened inside
`run()` — to avoid contention with the MCP server's request-handler
connection. WAL mode (set on connection open) lets concurrent reads
proceed while the worker writes.

Lifecycle is bound to the MCP server (see T4): `start()` on server-up
if `_needs_worker` resolves true, `stop()` on shutdown via the
`threading.Event` checked at every loop iteration.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import sqlite_vec

from . import extractor as _extractor
from . import llm as _llm
from . import store as _store


logger = logging.getLogger("sage_memory.worker")


# ─── Tunables ─────────────────────────────────────────────────────

_STALE_RUNNING_SECONDS = 300        # 5 min — ADR-003 §Worker startup recovery
_REEMBED_MAX_BATCHES = 32           # caps a single reembed task; protects
                                    # shutdown bound and prevents a runaway
                                    # task from monopolizing the worker.
                                    # Per default embed_pending batch=50,
                                    # this covers up to 1600 chunks — well
                                    # above the chunker's 200/memory cap.

# M5 T0: extraction_queue retention per ADR-001 + ADR-003.
_QUEUE_RETENTION_SECONDS = 30 * 86400   # 30 days
_PRUNE_INTERVAL_SECONDS = 86400          # 24 hours


# ─── Errors that escape dispatch but are handled at loop level ────


class _TaskFailed(Exception):
    """Internal: caught at loop level, marks task failed."""


# ─── Worker ───────────────────────────────────────────────────────


class Worker:
    """Background queue worker.

    Per spec rev 2 pin #5: callers MUST pass a file-backed db_path
    (string or Path). The worker opens its own sqlite connection
    inside `run()` and cannot share an `:memory:` connection.

    **Shutdown bound (revised post-llm.py timeout bump, 2026-05-17):**
    `stop()` joins the worker thread with `shutdown_timeout_s` (default
    45s). The bound depends on what the worker is doing when stop fires:

    - Idle / between tasks: ~poll_interval (default 1s)
    - Mid-LLM-call (single attempt): up to
      `httpx connect (5s) + httpx read (30s)` = 35s
    - Mid-retry-sleep: up to 30s extra (Retry-After cap)
    - Worst case across 3 retries: ~165s

    The single-call worst case (35s) fits within the 45s default. The
    retry-sleep windows are NOT interruptible by `stop_event` —
    `time.sleep()` inside `_post_with_retry` does not check the event.
    A daemon-thread guard (`daemon=True` at thread spawn) ensures the
    OS reaps any in-flight worker on process exit even if `join()`
    returned with the thread still alive (a WARNING is logged in
    that case). M4 may revisit by passing a stop_event into the LLM
    retry loop for interruptible sleeps.
    """

    def __init__(
        self,
        db_path: str,
        *,
        poll_interval_ms: int = 1000,
        shutdown_timeout_s: float = 45.0,
    ) -> None:
        self._db_path = str(db_path)
        self._poll_interval = poll_interval_ms / 1000.0
        self._shutdown_timeout = shutdown_timeout_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ─── Public API ──────────────────────────────────────────────

    def start(self) -> None:
        """Idempotent. If a previous thread exists but exited
        (killed externally OR exhausted from a stop()), creates a
        fresh thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="sage-memory-worker", daemon=True,
        )
        self._thread.start()
        logger.info("worker: started (db_path=%s)", self._db_path)

    def stop(self) -> None:
        """Signal stop + join. Bounded by shutdown_timeout_s."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=self._shutdown_timeout)
        if self._thread.is_alive():
            logger.warning(
                "worker: join timed out after %.1fs; thread still alive",
                self._shutdown_timeout,
            )
        else:
            # Successful join — null the handle so re-calls of stop()
            # don't re-touch a dead thread (cosmetic; harmless either
            # way) and so a future start() reads clean state.
            self._thread = None
        logger.info("worker: stopped")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def maybe_prune(self) -> bool:
        """M5 T0: prune extraction_queue if 24h has passed since the
        last prune (or never pruned). Reads + updates
        `worker_state.last_prune_at`. Returns True iff prune executed
        (regardless of how many rows were deleted), False if skipped
        due to the 24h window.

        Per ADR-001 + ADR-003: deletes `done`/`failed` rows whose
        `processed_at` is older than 30 days.
        """
        conn = _open_worker_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT last_prune_at FROM worker_state WHERE id = 1"
            ).fetchone()
            last = row["last_prune_at"] if row else None
            now = time.time()
            if last is not None and (now - last) < _PRUNE_INTERVAL_SECONDS:
                return False
            cutoff = now - _QUEUE_RETENTION_SECONDS
            cur = conn.execute(
                "DELETE FROM extraction_queue "
                "WHERE status IN ('done', 'failed') "
                "  AND processed_at < ?",
                (cutoff,),
            )
            conn.execute(
                "UPDATE worker_state SET last_prune_at = ? WHERE id = 1",
                (now,),
            )
            conn.commit()
            if cur.rowcount:
                logger.info(
                    "worker: pruned %d aged extraction_queue rows",
                    cur.rowcount,
                )
            return True
        finally:
            conn.close()

    def drain_once(
        self, max_iterations: int = 100, timeout_s: float = 5.0,
    ) -> int:
        """TEST-ONLY synchronous drain. Returns count processed.

        Stop conditions (returns on ANY of):
          (a) `extraction_queue WHERE status IN ('pending','running')`
              count = 0
          (b) `max_iterations` tasks processed
          (c) `timeout_s` wall-clock elapsed

        NOT called from production code paths.
        """
        conn = _open_worker_conn(self._db_path)
        try:
            self._startup_recovery(conn)
            deadline = time.time() + timeout_s
            processed = 0
            while processed < max_iterations and time.time() < deadline:
                if self._queue_empty(conn):
                    break
                row = self._claim_one(conn)
                if row is None:
                    break
                self._dispatch(conn, row)
                processed += 1
            return processed
        finally:
            conn.close()

    def _wait_for_queue_empty(self, timeout_s: float = 2.0) -> bool:
        """Polls a fresh conn until the queue is fully drained or
        timeout elapses. Returns True if drained."""
        deadline = time.time() + timeout_s
        # Open a short-lived conn for polling (cheap).
        conn = _open_worker_conn(self._db_path)
        try:
            while time.time() < deadline:
                if self._queue_empty(conn):
                    return True
                time.sleep(0.05)
            return self._queue_empty(conn)
        finally:
            conn.close()

    # ─── Thread body ─────────────────────────────────────────────

    def _run(self) -> None:
        conn = _open_worker_conn(self._db_path)
        try:
            self._startup_recovery(conn)
            conn.close()  # release file lock before maybe_prune opens its own
            # M5 T0: prune on startup. Opens its own conn (idempotent).
            self.maybe_prune()
            conn = _open_worker_conn(self._db_path)
            while not self._stop_event.is_set():
                row = self._claim_one(conn)
                if row is None:
                    # M5 T0: poll-loop prune check. maybe_prune is cheap
                    # when within the 24h window (single SELECT).
                    # Release conn around maybe_prune to avoid lock
                    # contention with its own opened conn.
                    conn.close()
                    self.maybe_prune()
                    conn = _open_worker_conn(self._db_path)
                    self._stop_event.wait(timeout=self._poll_interval)
                    continue
                self._dispatch(conn, row)
        finally:
            conn.close()

    def _startup_recovery(self, conn) -> None:
        """Reset stale 'running' rows back to 'pending' (ADR-003)."""
        cur = conn.execute(
            "UPDATE extraction_queue "
            "SET status = 'pending', started_at = NULL "
            "WHERE status = 'running' "
            "  AND started_at IS NOT NULL "
            "  AND started_at < (unixepoch() - ?)",
            (_STALE_RUNNING_SECONDS,),
        )
        if cur.rowcount:
            logger.info(
                "worker: startup recovery reset %d stale running rows",
                cur.rowcount,
            )
        conn.commit()

    def _queue_empty(self, conn) -> bool:
        n = conn.execute(
            "SELECT COUNT(*) FROM extraction_queue "
            "WHERE status IN ('pending', 'running')"
        ).fetchone()[0]
        return n == 0

    def _claim_one(self, conn):
        """Optimistic claim. Returns the claimed row dict, or None if
        no pending row or another worker beat us (rowcount==0)."""
        row = conn.execute(
            "SELECT id, memory_id, task_type, attempts "
            "FROM extraction_queue "
            "WHERE status = 'pending' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE extraction_queue "
            "SET status = 'running', started_at = unixepoch(), "
            "    attempts = attempts + 1 "
            "WHERE id = ? AND status = 'pending'",
            (row["id"],),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Lost the race (hypothetical in single-process)
            return None
        return row

    def _dispatch(self, conn, row) -> None:
        task_id = row["id"]
        task_type = row["task_type"]
        memory_id = row["memory_id"]
        try:
            if task_type == "extract":
                self._do_extract(conn, memory_id)
            elif task_type == "reembed":
                self._do_reembed(conn, memory_id)
            elif task_type == "dedup":
                self._do_dedup(conn)
            else:
                raise _TaskFailed(
                    f"unknown task_type: {task_type!r}"
                )
            self._mark_done(conn, task_id)
        except _TaskFailed as e:
            self._mark_failed(conn, task_id, str(e))
        except _llm.LlmNotConfiguredError as e:
            self._mark_failed(conn, task_id, str(e))
        except _extractor.ExtractionFailedError as e:
            self._mark_failed(conn, task_id, str(e))
        except Exception as e:
            logger.error(
                "worker: task %s raised %s: %s",
                task_id, type(e).__name__, e, exc_info=True,
            )
            self._mark_failed(conn, task_id, str(e))

    # ─── Task implementations ────────────────────────────────────

    def _do_extract(self, conn, memory_id) -> None:
        content_row = conn.execute(
            "SELECT content FROM memories WHERE id = ?", (memory_id,),
        ).fetchone()
        if content_row is None or content_row["content"] is None:
            raise _TaskFailed("memory not found")
        content = content_row["content"]

        result = _extractor.extract(content)
        now = time.time()

        # Upsert entities + write mentions + relations in a single tx.
        # Each entity is found-or-created via UPSERT on
        # UNIQUE(name_normalized, type) — the dedup key from migration 004.
        name_to_id: dict[tuple[str, str], str] = {}
        for ent in result["entities"]:
            normalized = _extractor.normalize_name(ent["name"])
            etype = ent["type"]
            entity_id = self._upsert_entity(
                conn, ent["name"], normalized, etype, now,
            )
            name_to_id[(normalized, etype)] = entity_id
            self._insert_mention(
                conn, memory_id, entity_id,
                ent.get("surface_form", ent["name"]), content, now,
            )

        for rel in result["relations"]:
            src_norm = _extractor.normalize_name(rel["source_name"])
            tgt_norm = _extractor.normalize_name(rel["target_name"])
            # Find candidate entity ids by normalized name; pick the
            # one we wrote in this batch if available, else any
            # existing entity. If neither exists, skip (relation
            # without resolved endpoints isn't useful).
            src_id = self._resolve_entity_id(conn, src_norm, name_to_id)
            tgt_id = self._resolve_entity_id(conn, tgt_norm, name_to_id)
            if src_id is None or tgt_id is None:
                continue
            self._insert_relation(
                conn, src_id, tgt_id, rel["type"], memory_id, now,
            )
        conn.commit()

    def _upsert_entity(
        self, conn, name, normalized, etype, now,
    ) -> str:
        # Try insert with new uuid; on conflict, increment mention_count
        # and return the existing id. Using ON CONFLICT lets us avoid
        # a separate SELECT.
        new_id = uuid.uuid4().hex
        cur = conn.execute(
            "INSERT INTO entities "
            "(id, name, name_normalized, type, mention_count, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(name_normalized, type) DO UPDATE SET "
            "  mention_count = mention_count + 1, "
            "  updated_at = excluded.updated_at "
            "RETURNING id",
            (new_id, name, normalized, etype, now, now),
        )
        row = cur.fetchone()
        return row["id"]

    def _insert_mention(
        self, conn, memory_id, entity_id, surface_form, content, now,
    ) -> None:
        # First-match offset; -1 → NULL.
        start = content.find(surface_form)
        if start == -1:
            context_start = None
            context_end = None
        else:
            context_start = start
            context_end = start + len(surface_form)
        try:
            conn.execute(
                "INSERT INTO mentions "
                "(memory_id, entity_id, surface_form, "
                " context_start, context_end, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
                (memory_id, entity_id, surface_form,
                 context_start, context_end, now),
            )
        except sqlite3.IntegrityError:
            # PRIMARY KEY (memory_id, entity_id, surface_form) hit on
            # idempotent replay — silent skip per spec.
            pass

    def _resolve_entity_id(
        self, conn, normalized: str, name_to_id: dict,
    ) -> str | None:
        # Try the just-written entities first (by normalized name).
        for (nn, _t), eid in name_to_id.items():
            if nn == normalized:
                return eid
        # Fall back to any existing entity with this normalized name.
        row = conn.execute(
            "SELECT id FROM entities WHERE name_normalized = ? "
            "ORDER BY mention_count DESC LIMIT 1",
            (normalized,),
        ).fetchone()
        return row["id"] if row else None

    def _insert_relation(
        self, conn, src_id, tgt_id, rtype, source_memory_id, now,
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO relations "
                "(id, source_entity_id, target_entity_id, relation_type, "
                " source_memory_id, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
                (uuid.uuid4().hex, src_id, tgt_id, rtype,
                 source_memory_id, now),
            )
        except sqlite3.IntegrityError:
            # UNIQUE(source_entity_id, target_entity_id, relation_type,
            # source_memory_id) — replay-safe skip.
            pass

    def _do_dedup(self, conn) -> None:
        """M5 T3: worker dedup task type. Calls the shared algorithm
        in `dedup.run_pass`. LLM-key required; absent → _TaskFailed
        marks the task `failed` + logs structured reason.
        """
        from . import dedup as _dedup_mod
        if not _llm.is_configured():
            raise _TaskFailed(
                "dedup task: no LLM key configured "
                "(ANTHROPIC_API_KEY / OPENAI_API_KEY)"
            )
        summary = _dedup_mod.run_pass(conn, llm_confirm=True)
        logger.info(
            "worker: dedup pass complete (considered=%d, merged=%d, "
            "est_cost=$%.4f)",
            summary["pairs_considered"], summary["pairs_merged"],
            summary["cost_estimate_usd"],
        )

    def _do_reembed(self, conn, memory_id) -> None:
        # Per rev 2 pin #1, reembed is per-memory: drain memory's
        # memory-level vec + ALL chunk vec rows via T6a's memory_id
        # filter. `embed_pending*` are batch-limited (default 50),
        # so a memory with >50 stale chunks needs multiple calls.
        # Loop until both return 0 — but break early on stop_event
        # so shutdown isn't blocked by a giant reembed.
        for _ in range(_REEMBED_MAX_BATCHES):
            if self._stop_event.is_set():
                break
            n_mem = _store.embed_pending(conn, memory_id=memory_id)
            n_chk = _store.embed_pending_chunks(
                conn, memory_id=memory_id,
            )
            if n_mem == 0 and n_chk == 0:
                break

    # ─── Status transitions ──────────────────────────────────────

    def _mark_done(self, conn, task_id) -> None:
        conn.execute(
            "UPDATE extraction_queue "
            "SET status = 'done', processed_at = unixepoch() "
            "WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        logger.debug("worker: task %s done", task_id)

    def _mark_failed(self, conn, task_id, last_error: str) -> None:
        conn.execute(
            "UPDATE extraction_queue "
            "SET status = 'failed', processed_at = unixepoch(), "
            "    last_error = ? "
            "WHERE id = ?",
            (last_error, task_id),
        )
        conn.commit()
        logger.warning(
            "worker: task %s failed: %s", task_id, last_error,
        )


# ─── Worker-owned connection helper ──────────────────────────────


def _open_worker_conn(db_path: str) -> sqlite3.Connection:
    """Open a sqlite connection for worker use.

    Bypasses the module-level `_connections` cache in db.py so the
    worker thread genuinely has its own connection (per ADR-003
    §Failure Modes "worker uses its own SQLite connection").
    Mirrors `db._open` pragmas/extensions but does NOT run migrations
    — the DB must already be migrated by the time the worker starts.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn
