"""M3.3 — Routed write to a hub-registered project (``hub store --to``).

Per ADR-008 §"Federation semantics" + ADR-009 §5:
  1. Resolve the named project from ``~/.sage-hub.yaml``.
  2. Validate ``writable: true``.
  3. Acquire ownership (or verify we already hold it via the server's
     registry) — this gates the call against another server holding
     a fresh claim.
  4. Switch sage-memory's active project to the target, call the
     standard ``store.store()`` path, restore the prior state.
  5. Release ownership if we acquired it fresh (server-owned
     projects keep their long-lived token).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import config as _hub_config
from . import ownership as _hub_ownership


logger = logging.getLogger("sage_memory.hub.store")


def store_to_project(
    name: str,
    *,
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    entities: list[dict] | None = None,
    relations: list[dict] | None = None,
    hub_path: Path | None = None,
) -> dict[str, Any]:
    """Route a store call to ``name`` (a writable hub-registered project).

    Returns the standard store envelope ``{success, id, message, ...}``
    on success, or a ``{success: False, message: ...}`` envelope when
    routing fails. On success the new memory lives in the target
    project's DB, NOT the caller's active project.
    """
    cfg_path = hub_path or _hub_config.DEFAULT_HUB_PATH
    if not cfg_path.exists():
        return {
            "success": False,
            "message": (
                f"hub config not found at {cfg_path}. "
                f"Run `sage-memory hub init` first."
            ),
        }

    try:
        cfg = _hub_config.load(cfg_path)
    except Exception as exc:
        return {
            "success": False,
            "message": f"could not load hub config: {exc}",
        }

    project = next((p for p in cfg.projects if p.name == name), None)
    if project is None:
        return {
            "success": False,
            "message": f"no hub project named {name!r}",
        }
    if not project.writable:
        return {
            "success": False,
            "message": (
                f"project {name!r} is not writable. Re-add with "
                f"`sage-memory hub add ... --writable` to enable routed-write."
            ),
        }
    if not project.path.exists() or not project.path.is_dir():
        return {
            "success": False,
            "message": (
                f"project {name!r} path does not exist: {project.path}"
            ),
        }

    # Acquire ownership unless we (this process) already hold it via
    # the long-lived server registry.
    already_owned = str(project.path) in _hub_ownership.registry
    transient_token = None
    if not already_owned:
        transient_token = _hub_ownership.acquire(project.path)
        if transient_token is None:
            info = _hub_ownership.check(project.path)
            pid = info.owner_pid if info else "?"
            return {
                "success": False,
                "message": (
                    f"project {name!r} is hub-managed by PID {pid}; "
                    f"cannot route write."
                ),
            }

    try:
        return _write_via_project(
            project.path,
            content=content,
            title=title,
            tags=tags,
            entities=entities,
            relations=relations,
        )
    finally:
        if transient_token is not None:
            _hub_ownership.release(transient_token)


def _write_via_project(
    project_path: Path,
    *,
    content: str,
    title: str | None,
    tags: list[str] | None,
    entities: list[dict] | None,
    relations: list[dict] | None,
) -> dict[str, Any]:
    """Switch active project context, call store.store, restore state.

    sage-memory's `db.get_db()` reads a module-level active project.
    Routing requires temporarily pointing it at the target. This is
    safe for CLI calls (single-threaded one-shot) and for the M3.6
    MCP integration (single FastMCP event loop dispatching requests).
    """
    from .. import db as _db
    from ..store import store as _store_fn

    # Snapshot prior active state so we restore on return.
    prior_root = _db._active_project
    prior_set = _db._active_project_set

    try:
        _db.close_all()
        _db.override_project_root(project_path)
        _db._open(_db.get_project_db_path(project_path))
        return _store_fn(
            content=content,
            title=title,
            tags=tags,
            entities=entities,
            relations=relations,
        )
    finally:
        # Restore prior context so subsequent calls in this process
        # don't unexpectedly target the routed project.
        _db.close_all()
        if prior_set:
            _db.override_project_root(prior_root)
        else:
            _db.override_project_root(None)
            _db._active_project_set = False
