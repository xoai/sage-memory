"""M3.1 — PID-and-heartbeat ownership for hub-registered project DBs.

Per ADR-009 rev 2: a project DB that is hub-registered as
``writable: true`` AND is currently owned by a running server process
refuses writes from any other process. Reads remain unrestricted.

The state file ``<project>/.sage-memory/.hub-owner.json`` carries the
owner pid + heartbeat timestamp. Stale owners (heartbeat > 60s) are
reclaimable via the **atomic-rename protocol** (ADR-009 §3) that
closes the rev-1 race window where two would-be reclaimers both
saw the same stale file and both thought they won.

This is concurrent-writer SQLite safety, NOT access control. See
ADR-009 §"Scope clarification" for the layered story.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("sage_memory.hub.ownership")


# Ownership file lives at <project>/.sage-memory/.hub-owner.json.
_OWNER_FILENAME = ".hub-owner.json"
_SAGE_DIR = ".sage-memory"

# Stale window per ADR-009: 60s heartbeat tolerance gives generous
# room for laptop sleep / clock skew / NTP jumps on Docker volumes.
STALE_AFTER_SECONDS: float = 60.0

# Heartbeat default interval: ADR-009 says 20s.
HEARTBEAT_INTERVAL_SECONDS: float = 20.0

# Env-var escape hatch (ADR-009 §"Escape hatches"). Set to "1" in
# dev sessions that intentionally bypass the ownership check.
_BYPASS_ENV_VAR = "SAGE_HUB_IGNORE_OWNERSHIP"


@dataclass(frozen=True)
class OwnershipToken:
    """Returned by ``acquire`` when this process successfully takes
    ownership of a project DB. Pass to ``release`` / heartbeat tasks."""

    project_path: Path
    pid: int
    started_at: float


@dataclass(frozen=True)
class OwnershipInfo:
    """Read by ``check`` — what the on-disk state currently says
    (independent of whether this process is the owner)."""

    owner_pid: int
    owner_heartbeat: float
    project_path: Path


# Server-owned tokens — one per writable project the server holds.
registry: dict[str, OwnershipToken] = {}

# Per-DB disable flags populated by ``check()`` on cache miss. Keyed
# by ``str(project_path)`` so the lookup matches store.py's path
# resolution. Per-DB (NOT module-level boolean) per ADR-009 rev 2.
_disabled_writes: dict[str, OwnershipInfo] = {}


def _owner_file(project_path: Path) -> Path:
    return project_path / _SAGE_DIR / _OWNER_FILENAME


def _read_owner(owner_file: Path) -> OwnershipInfo | None:
    """Return the OwnershipInfo from the file, or None if absent /
    corrupt / missing required fields."""
    try:
        raw = json.loads(owner_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return OwnershipInfo(
            owner_pid=int(raw["owner_pid"]),
            owner_heartbeat=float(raw["owner_heartbeat"]),
            project_path=owner_file.parent.parent,
        )
    except (KeyError, TypeError, ValueError):
        return None


def acquire(
    project_path: Path,
    *,
    ttl_seconds: float = STALE_AFTER_SECONDS,
    now: float | None = None,
    _max_retries: int = 3,
) -> OwnershipToken | None:
    """Atomically take ownership of ``project_path``'s DB.

    Returns an ``OwnershipToken`` on success, ``None`` when another
    process holds a fresh ownership claim (heartbeat within
    ``ttl_seconds``). On a stale claim, attempts the atomic-rename
    reclaim protocol per ADR-009 rev 2.

    M3 review M4 fix: the file can vanish between the EXCL-create
    failure and the freshness check (legitimate release by the
    previous owner). Retry up to ``_max_retries`` times so the
    legitimate-release race doesn't surface as a spurious None.
    """
    owner_file = _owner_file(project_path)
    owner_file.parent.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    now = now if now is not None else time.time()

    payload = {
        "owner_pid": my_pid,
        "owner_started": now,
        "owner_heartbeat": now,
    }
    payload_bytes = json.dumps(payload).encode()

    for _attempt in range(_max_retries):
        # Step 1: atomic create-or-fail.
        try:
            fd = os.open(owner_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            fd = None
        if fd is not None:
            try:
                os.write(fd, payload_bytes)
            finally:
                os.close(fd)
            token = OwnershipToken(
                project_path=project_path, pid=my_pid, started_at=now,
            )
            registry[str(project_path)] = token
            return token

        # Step 2: file exists — check freshness.
        existing = _read_owner(owner_file)
        if existing is None:
            # File vanished or corrupt between EXCL-fail and read.
            # If vanished (legitimate release), retry the create.
            # If corrupt, ALSO retry — corrupt + EXCL-create race
            # is unlikely; the retry will either re-read corruption
            # (and fall through to "give up") or succeed on the
            # next pass.
            if not owner_file.exists():
                continue
            # Truly corrupt: don't reclaim silently. Operator should
            # use the rm escape hatch.
            return None
        if now - existing.owner_heartbeat <= ttl_seconds:
            return None  # Fresh owner; refuse to take.

        # Step 3: stale — atomic-rename reclaim.
        if _reclaim_stale(project_path, my_pid, now=now):
            token = OwnershipToken(
                project_path=project_path, pid=my_pid, started_at=now,
            )
            registry[str(project_path)] = token
            return token
        return None

    return None


def _reclaim_stale(
    project_path: Path,
    my_pid: int,
    *,
    now: float | None = None,
) -> bool:
    """Atomic-rename protocol (ADR-009 §3). Returns True iff this
    process is the rightful new owner.

    Two racing reclaimers both write their own candidate file. Both
    then ``os.replace`` it into the canonical owner_file. POSIX
    rename is atomic — the destination ends up with one writer's
    payload, never a merge. Read-after-write confirms which won.
    """
    owner_file = _owner_file(project_path)
    candidate = owner_file.with_name(
        f"{_OWNER_FILENAME}.pid{my_pid}.candidate"
    )
    now = now if now is not None else time.time()
    payload = {
        "owner_pid": my_pid,
        "owner_started": now,
        "owner_heartbeat": now,
    }
    try:
        candidate.write_text(json.dumps(payload))
    except OSError as exc:
        logger.warning(
            "ownership: could not write candidate file %s: %s",
            candidate, exc,
        )
        return False

    try:
        # os.replace is atomic on POSIX + best-effort atomic on Windows.
        os.replace(candidate, owner_file)
    except OSError as exc:
        logger.warning(
            "ownership: atomic-rename reclaim failed for %s: %s",
            owner_file, exc,
        )
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    # Read-after-write verification.
    winner = _read_owner(owner_file)
    return winner is not None and winner.owner_pid == my_pid


def release(token: OwnershipToken) -> None:
    """Voluntary release — delete the owner file + remove from registry.
    Idempotent: missing file / missing registry entry are both no-ops."""
    owner_file = _owner_file(token.project_path)
    try:
        owner_file.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "ownership: could not unlink %s: %s", owner_file, exc,
        )
    registry.pop(str(token.project_path), None)


def check(
    project_path: Path,
    *,
    ttl_seconds: float = STALE_AFTER_SECONDS,
    now: float | None = None,
) -> OwnershipInfo | None:
    """Read the on-disk ownership claim for ``project_path``.

    Returns ``OwnershipInfo`` if the claim is fresh (heartbeat within
    ``ttl_seconds``); ``None`` if absent, stale, or corrupt.
    Populates ``_disabled_writes`` as a side effect so the
    ``is_disabled_for`` fast path can skip the file read on hot
    write entry-points (ADR-009 §4 — "on first call to get_db()").
    """
    if os.environ.get(_BYPASS_ENV_VAR) == "1":
        return None
    owner_file = _owner_file(project_path)
    info = _read_owner(owner_file)
    if info is None:
        _disabled_writes.pop(str(project_path), None)
        return None
    now = now if now is not None else time.time()
    if now - info.owner_heartbeat > ttl_seconds:
        _disabled_writes.pop(str(project_path), None)
        return None
    # Only mark as disabled when the owner is a DIFFERENT process.
    # The server that holds the token reads its own claim and must
    # remain a writer.
    if info.owner_pid != os.getpid():
        _disabled_writes[str(project_path)] = info
    return info


def is_disabled_for(
    project_path: Path, *, now: float | None = None,
) -> bool:
    """Does this process see ``project_path`` as owned by another fresh
    process? Per-DB (not module-level) per ADR-009 rev 2.

    M3 review M1 fix: re-validates freshness on the cached entry on
    each call. Without this, a cached entry would report disabled
    forever even after the owner's heartbeat went stale — the
    "wait for stale reclaim" escape hatch in the error message
    only worked for callers that hadn't yet populated the cache.
    """
    if os.environ.get(_BYPASS_ENV_VAR) == "1":
        return False
    key = str(project_path)
    cached = _disabled_writes.get(key)
    if cached is None:
        return False
    now = now if now is not None else time.time()
    if now - cached.owner_heartbeat > STALE_AFTER_SECONDS:
        # Cache entry is stale per its own heartbeat — drop it and
        # let the caller proceed (or re-check via check()).
        _disabled_writes.pop(key, None)
        return False
    return True


def _write_heartbeat(
    token: OwnershipToken, *, now: float | None = None,
) -> bool:
    """Update ``owner_heartbeat`` on the file, but only if this
    process is still the recorded owner (ADR-009 §"Heartbeat loop"
    rev 3 — laptop-sleep edge case).

    Returns True if the heartbeat write succeeded; False if we've
    been displaced (file vanished / pid mismatch).
    """
    owner_file = _owner_file(token.project_path)
    info = _read_owner(owner_file)
    if info is None or info.owner_pid != token.pid:
        return False
    new_now = now if now is not None else time.time()
    payload = {
        "owner_pid": token.pid,
        "owner_started": token.started_at,
        "owner_heartbeat": new_now,
    }
    try:
        owner_file.write_text(json.dumps(payload))
    except OSError as exc:
        logger.warning(
            "ownership: heartbeat write failed for %s: %s",
            owner_file, exc,
        )
        return False
    return True


async def heartbeat_loop(
    token: OwnershipToken,
    *,
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Refresh the owner file's heartbeat every ``interval`` seconds.

    Designed to live as one ``asyncio.Task`` per held token under
    FastMCP's ``lifespan=`` context. Cancellation (graceful shutdown)
    triggers ``CancelledError`` → token release → file deleted.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            if not _write_heartbeat(token):
                # We've been displaced (laptop-sleep edge case).
                # Stop heartbeating quietly; the held token is now
                # a no-op until manually cleared.
                logger.warning(
                    "ownership: heartbeat displaced for %s; "
                    "stopping heartbeat task",
                    token.project_path,
                )
                return
    except asyncio.CancelledError:
        release(token)
        raise


def block_envelope_if_disabled(scope: str = "project") -> dict | None:
    """Convenience for store/update/delete/link entry-points (M3.2).

    Resolves the active connection for ``scope`` (defaults to the
    project DB), maps its filesystem path back to a project root,
    and returns a read-only error envelope if that project is
    hub-owned by a fresh other-process owner. Otherwise returns
    ``None`` (and the caller proceeds as normal).

    No-op for:
      - scope="global" (the global DB is never hub-registered)
      - sessions with the ``SAGE_HUB_IGNORE_OWNERSHIP=1`` env var
      - projects with no ``.hub-owner.json`` file
      - projects whose owner file is stale (heartbeat > 60s)
      - projects owned by THIS process (via ``acquire``)
    """
    from .. import db as _db  # lazy: avoid circular import at module load
    try:
        conn = _db.get_db(scope)
        db_path = _db.get_db_path(conn)
    except Exception:
        return None
    if db_path == _db.get_global_db_path().resolve():
        return None  # global isn't hub-registered

    # db_path layout: <project>/.sage-memory/memory.db → project = parent.parent
    project_root = db_path.parent.parent

    # Always re-read the owner file on each write entry — the cache
    # alone can't detect external state changes (file rewritten with
    # a stale heartbeat, file deleted, new owner takes over).
    # M3 review M1 fix: pre-fix code only populated the cache lazily
    # then trusted it, so the "wait for stale reclaim" escape hatch
    # advertised in the error message didn't work for cached callers.
    check(project_root)
    if not is_disabled_for(project_root):
        return None

    info = _disabled_writes.get(str(project_root))
    pid = info.owner_pid if info else "?"
    return {
        "success": False,
        "message": (
            f"project {project_root} is hub-managed by PID {pid}; "
            f"writes are disabled in this session. To take over, "
            f"stop the server or wait for stale reclaim "
            f"({int(STALE_AFTER_SECONDS)}s)."
        ),
    }


def reset_module_state_for_tests() -> None:
    """Clear the module-level registries. Tests use this between
    cases to avoid state leak — never call in production code."""
    registry.clear()
    _disabled_writes.clear()
