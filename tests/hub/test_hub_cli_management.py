"""M2.2 — cli_hub.py management subcommands (init / add / remove / list / status).

8 tests per plan.md M2.2 done-when:
  - 5 happy-path tests (one per subcommand)
  - 3 negatives: invalid name, missing required arg, non-existent project path
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml


# ─── Test helpers ─────────────────────────────────────────────────


def _make_project_dir(tmp_path: Path, name: str = "alpha") -> Path:
    """Create a directory that looks like a sage-memory project root."""
    d = tmp_path / name
    d.mkdir()
    # Stamp a marker so `hub add` recognises it as a project root.
    (d / ".git").mkdir()
    return d


def _make_project_with_memory_db(tmp_path: Path, name: str = "alpha") -> Path:
    """Project root with a real (empty) .sage-memory/memory.db so
    `hub status` can read stats."""
    d = _make_project_dir(tmp_path, name)
    sm = d / ".sage-memory"
    sm.mkdir()
    db_path = sm / "memory.db"
    # Minimal DB so 'hub status' can open + count rows; the table
    # shape doesn't have to be production-real for the count probe.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)"
    )
    conn.execute(
        "INSERT INTO memories (id, content) VALUES ('a', 'hi')"
    )
    conn.execute(
        "INSERT INTO memories (id, content) VALUES ('b', 'there')"
    )
    conn.commit()
    conn.close()
    return d


# ─── 1. hub init creates empty config file ────────────────────────


def test_hub_init_creates_empty_config_file(tmp_path, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    rc = run_hub(["init", "--config-path", str(hub_path)])
    assert rc == 0
    assert hub_path.exists()

    data = yaml.safe_load(hub_path.read_text())
    assert data == {"version": 1, "projects": []}


# ─── 2. hub add a valid project ───────────────────────────────────


def test_hub_add_appends_typed_record_with_defaults(tmp_path):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    project = _make_project_dir(tmp_path, "backend")

    assert run_hub(["init", "--config-path", str(hub_path)]) == 0
    rc = run_hub([
        "add", str(project),
        "--config-path", str(hub_path),
    ])
    assert rc == 0

    data = yaml.safe_load(hub_path.read_text())
    assert len(data["projects"]) == 1
    p = data["projects"][0]
    # Auto-generated name from basename per ADR-008.
    assert p["name"] == "backend"
    assert p["path"] == str(project)
    # Defaults per ADR-008 §"Default flag semantics on hub add".
    assert p["searchable"] is True
    assert p["writable"] is False


# ─── 3. hub remove drops named project ────────────────────────────


def test_hub_remove_filters_out_named_project(tmp_path):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    project = _make_project_dir(tmp_path, "ops")

    assert run_hub(["init", "--config-path", str(hub_path)]) == 0
    assert run_hub([
        "add", str(project),
        "--config-path", str(hub_path),
    ]) == 0
    rc = run_hub([
        "remove", "ops",
        "--config-path", str(hub_path),
    ])
    assert rc == 0

    data = yaml.safe_load(hub_path.read_text())
    assert data["projects"] == []


# ─── 4. hub list outputs registered projects ──────────────────────


def test_hub_list_prints_registered_project_names(tmp_path, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    project = _make_project_dir(tmp_path, "frontend")

    run_hub(["init", "--config-path", str(hub_path)])
    run_hub(["add", str(project), "--config-path", str(hub_path)])
    capsys.readouterr()  # discard add output

    rc = run_hub(["list", "--config-path", str(hub_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "frontend" in out
    assert str(project) in out


# ─── 5. hub status reports per-project stats ──────────────────────


def test_hub_status_prints_memory_count_and_size(tmp_path, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    project = _make_project_with_memory_db(tmp_path, "data")

    run_hub(["init", "--config-path", str(hub_path)])
    run_hub(["add", str(project), "--config-path", str(hub_path)])
    capsys.readouterr()

    rc = run_hub(["status", "--config-path", str(hub_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "data" in out
    # Memory count visible in output — we seeded 2 rows.
    assert "2" in out


# ─── 6. hub add: invalid name rejected ────────────────────────────


def test_hub_add_invalid_name_rejected(tmp_path, capsys):
    """Project names must be safe slugs (alphanum + dash + underscore)
    — names with slashes / spaces would break the YAML lookup path
    and lead to confusing CLI UX. Reject explicitly."""
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    project = _make_project_dir(tmp_path, "alpha")

    run_hub(["init", "--config-path", str(hub_path)])
    capsys.readouterr()

    rc = run_hub([
        "add", str(project),
        "--name", "bad/name",
        "--config-path", str(hub_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2, f"expected exit 2, got {rc}; stderr={err!r}"
    assert "name" in err.lower()


# ─── 7. hub add: missing required positional arg ──────────────────


def test_hub_add_missing_path_rejected(tmp_path, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    run_hub(["init", "--config-path", str(hub_path)])
    capsys.readouterr()

    rc = run_hub(["add", "--config-path", str(hub_path)])
    err = capsys.readouterr().err
    assert rc == 2, f"expected exit 2, got {rc}; stderr={err!r}"
    assert "path" in err.lower()


# ─── 8. hub add: non-existent path errors clearly ─────────────────


def test_hub_add_nonexistent_path_errors(tmp_path, capsys):
    from sage_memory.cli_hub import run_hub

    hub_path = tmp_path / ".sage-hub.yaml"
    run_hub(["init", "--config-path", str(hub_path)])
    capsys.readouterr()

    missing = tmp_path / "no-such-project"
    rc = run_hub([
        "add", str(missing),
        "--config-path", str(hub_path),
    ])
    err = capsys.readouterr().err
    assert rc == 2, f"expected exit 2, got {rc}; stderr={err!r}"
    assert str(missing) in err
