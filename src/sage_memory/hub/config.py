"""``~/.sage-hub.yaml`` load/save + schema-evolution policy (ADR-008).

The file shape (populated):

  version: 1
  projects:
    - name: backend
      path: /Users/alice/projects/backend
      searchable: true
      writable: false
    - name: ops
      path: /Users/alice/projects/ops
      searchable: true
      writable: true

Defaults on ``add_project``: ``searchable=True`` (matches sage-wiki
``hub.go:122-126``), ``writable=False`` (safer; explicit opt-in
required for routed-write targets per ADR-008 §"Default flag
semantics on hub add").

The ``version`` field is the only stability guarantee. Future schema
bumps land via ``MIGRATIONS[(from_v, to_v)]`` callables in this module.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


logger = logging.getLogger("sage_memory.hub.config")


# Bumped each time the on-disk YAML schema gains a new shape. Loading
# a file with version > LATEST_KNOWN_VERSION is a refuse-to-load
# error: the operator must upgrade sage-memory.
LATEST_KNOWN_VERSION: int = 1


# Default config path. Tests pass an explicit path so they don't
# touch the user's real ~/.sage-hub.yaml.
DEFAULT_HUB_PATH: Path = Path.home() / ".sage-hub.yaml"


# Per-version migration hooks. Keys are ``(from_version, to_version)``
# tuples; values are callables ``(HubConfig) -> HubConfig``. v1 has
# no migrations yet but the registry exists so future bumps don't
# need to re-architect ``load``.
MIGRATIONS: dict[tuple[int, int], Callable[["HubConfig"], "HubConfig"]] = {}


class UnsupportedHubVersionError(RuntimeError):
    """Raised when the on-disk hub config's ``version`` exceeds the
    current ``LATEST_KNOWN_VERSION``. Refuse-to-load is safer than
    warn-but-load because a version > LATEST file may carry fields
    whose semantics our code doesn't understand — silently ignoring
    them could corrupt user intent."""


@dataclass(frozen=True)
class HubProject:
    """One registered project in ``~/.sage-hub.yaml``."""

    name: str
    path: Path
    searchable: bool = True
    writable: bool = False


@dataclass
class HubConfig:
    """In-memory representation of ``~/.sage-hub.yaml``."""

    version: int = LATEST_KNOWN_VERSION
    projects: list[HubProject] = field(default_factory=list)


def init(path: Path | None = None) -> HubConfig:
    """Create an empty hub config and write it to ``path``.

    Returns the in-memory representation. Used by ``sage-memory hub init``.
    """
    target = Path(path) if path is not None else DEFAULT_HUB_PATH
    cfg = HubConfig(version=LATEST_KNOWN_VERSION, projects=[])
    save(cfg, target)
    return cfg


def load(path: Path | None = None) -> HubConfig:
    """Parse a hub config from disk, applying schema-evolution rules.

    - Missing ``version`` field → assume ``1`` (one-time upgrade
      written back on the next ``save``).
    - ``version > LATEST_KNOWN_VERSION`` → raise
      ``UnsupportedHubVersionError`` with an operator-actionable
      message.
    - ``version < LATEST_KNOWN_VERSION`` → chain ``MIGRATIONS``
      hooks from the loaded version up to current.
    """
    target = Path(path) if path is not None else DEFAULT_HUB_PATH

    if not target.exists():
        raise FileNotFoundError(
            f"hub config not found at {target}. "
            f"Run `sage-memory hub init` to create one."
        )

    raw = yaml.safe_load(target.read_text()) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"hub config at {target} is not a YAML mapping: "
            f"got {type(raw).__name__}"
        )

    version = raw.get("version")
    if version is None:
        # ADR-008: missing version → assume 1, rewrite on next save.
        version = 1
    if not isinstance(version, int):
        raise RuntimeError(
            f"hub config at {target} has non-integer version: "
            f"{version!r}"
        )

    if version > LATEST_KNOWN_VERSION:
        raise UnsupportedHubVersionError(
            f"{target} has version {version}; this sage-memory understands "
            f"up to version {LATEST_KNOWN_VERSION}. Upgrade sage-memory or "
            f"downgrade the file by removing the version field."
        )

    projects = _projects_from_raw(raw.get("projects") or [])
    cfg = HubConfig(version=version, projects=projects)

    if version < LATEST_KNOWN_VERSION:
        cfg = _apply_migrations(cfg)

    return cfg


def save(config: HubConfig, path: Path | None = None) -> None:
    """Write the hub config to disk atomically.

    Atomic = write to a sibling ``<path>.tmp.<pid>``, then
    ``os.replace`` to the destination. Guarantees no partial writes
    visible to a concurrent reader; the last writer wins under a
    race (acceptable per ADR-008 — concurrent edits to the same hub
    config are rare; the consequence of "lost update" is one
    re-run of ``hub add``).
    """
    target = Path(path) if path is not None else DEFAULT_HUB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": config.version,
        "projects": [
            {
                "name": p.name,
                "path": str(p.path),
                "searchable": p.searchable,
                "writable": p.writable,
            }
            for p in config.projects
        ],
    }

    # Atomic write: write to a temp file in the same directory, then
    # rename. Same-directory rename is atomic on POSIX (and
    # best-effort atomic on Windows via os.replace).
    fd, tmp = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False)
        os.replace(tmp, target)
    except Exception:
        # Clean up the temp file if anything failed before rename.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_project(
    config: HubConfig,
    name: str,
    project_path: Path,
    *,
    searchable: bool = True,
    writable: bool = False,
) -> HubConfig:
    """Return a new ``HubConfig`` with the project appended.

    Defaults follow ADR-008: searchable=True, writable=False.
    Does NOT save — caller invokes ``save()`` explicitly so a CLI
    can batch multiple mutations before persisting.
    """
    if any(p.name == name for p in config.projects):
        raise ValueError(
            f"hub already has a project named {name!r}; "
            f"remove it first or choose a different name."
        )
    new_project = HubProject(
        name=name,
        path=Path(project_path),
        searchable=searchable,
        writable=writable,
    )
    return HubConfig(
        version=config.version,
        projects=[*config.projects, new_project],
    )


def remove_project(config: HubConfig, name: str) -> HubConfig:
    """Return a new ``HubConfig`` with the named project filtered out.

    Raises ``KeyError`` if the name isn't registered — keeps the CLI
    layer responsible for the user-facing error message shape.
    """
    if not any(p.name == name for p in config.projects):
        raise KeyError(f"no hub project named {name!r}")
    return HubConfig(
        version=config.version,
        projects=[p for p in config.projects if p.name != name],
    )


def _projects_from_raw(raw: Any) -> list[HubProject]:
    if not isinstance(raw, list):
        raise RuntimeError(
            f"hub config 'projects' field is not a list: "
            f"got {type(raw).__name__}"
        )
    out: list[HubProject] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"hub config project entry is not a mapping: {entry!r}"
            )
        name = entry.get("name")
        path = entry.get("path")
        if not name or not path:
            raise RuntimeError(
                f"hub config project entry missing name or path: {entry!r}"
            )
        out.append(HubProject(
            name=str(name),
            path=Path(str(path)),
            searchable=bool(entry.get("searchable", True)),
            writable=bool(entry.get("writable", False)),
        ))
    return out


def _apply_migrations(cfg: HubConfig) -> HubConfig:
    """Chain registered migrations from ``cfg.version`` up to current."""
    current = cfg
    while current.version < LATEST_KNOWN_VERSION:
        step = (current.version, current.version + 1)
        hook = MIGRATIONS.get(step)
        if hook is None:
            raise RuntimeError(
                f"no migration registered for hub config "
                f"{current.version} → {current.version + 1}; "
                f"cannot upgrade {cfg}"
            )
        current = hook(current)
    return current
