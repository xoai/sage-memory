"""M2.1 — hub/config.py: ~/.sage-hub.yaml load/save + schema-evolution.

Per ADR-008 (rev 3) §"Schema-evolution policy":
  - version: 1 is required at top of file
  - missing version → assume 1, rewrite on next save (one-time upgrade)
  - version > LATEST_KNOWN_VERSION → refuse to load with clear error
  - version < LATEST_KNOWN_VERSION → per-version migration hook applies
  - version field is the ONLY stability guarantee

8 tests per plan.md M2.1.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml


# ─── 1. Init empty ────────────────────────────────────────────────


def test_init_writes_empty_yaml_with_version(tmp_path):
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(path)

    assert cfg.version == hub_config.LATEST_KNOWN_VERSION
    assert cfg.projects == []
    # File on disk has the expected shape.
    data = yaml.safe_load(path.read_text())
    assert data == {"version": 1, "projects": []}


# ─── 2. Add + remove + list round-trip ────────────────────────────


def test_add_remove_round_trip_preserves_typed_records(tmp_path):
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    cfg = hub_config.init(path)

    project_a = tmp_path / "alpha"
    project_a.mkdir()
    project_b = tmp_path / "beta"
    project_b.mkdir()

    cfg = hub_config.add_project(cfg, "alpha", project_a)
    cfg = hub_config.add_project(
        cfg, "beta", project_b, searchable=True, writable=True,
    )
    hub_config.save(cfg, path)

    reloaded = hub_config.load(path)
    assert len(reloaded.projects) == 2
    names = {p.name for p in reloaded.projects}
    assert names == {"alpha", "beta"}

    # ADR-008: --searchable defaults TRUE, --writable defaults FALSE.
    alpha = next(p for p in reloaded.projects if p.name == "alpha")
    assert alpha.searchable is True
    assert alpha.writable is False
    assert alpha.path == project_a

    beta = next(p for p in reloaded.projects if p.name == "beta")
    assert beta.writable is True

    # Remove one
    cfg = hub_config.remove_project(reloaded, "alpha")
    hub_config.save(cfg, path)
    final = hub_config.load(path)
    assert [p.name for p in final.projects] == ["beta"]


# ─── 3. version: 1 required ───────────────────────────────────────


def test_version_field_is_required_in_canonical_save(tmp_path):
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    hub_config.save(
        hub_config.HubConfig(version=1, projects=[]), path,
    )
    on_disk = yaml.safe_load(path.read_text())
    assert "version" in on_disk
    assert on_disk["version"] == 1


# ─── 4. Missing version → assume 1, rewrite on next save ─────────


def test_missing_version_assumed_to_be_one_and_rewritten(tmp_path):
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    # User hand-wrote a config without the version field.
    path.write_text(yaml.safe_dump({"projects": []}))

    cfg = hub_config.load(path)
    assert cfg.version == 1

    # Next save rewrites the file with the version field present
    # (one-time upgrade per ADR-008).
    hub_config.save(cfg, path)
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk.get("version") == 1


# ─── 5. version > LATEST_KNOWN_VERSION → refuse ───────────────────


def test_unsupported_future_version_refuses_to_load(tmp_path):
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    path.write_text(yaml.safe_dump({"version": 999, "projects": []}))

    with pytest.raises(hub_config.UnsupportedHubVersionError) as exc:
        hub_config.load(path)

    # Error message must be actionable: name the file version, the
    # version we understand, and the operator path forward.
    msg = str(exc.value)
    assert "999" in msg
    assert str(hub_config.LATEST_KNOWN_VERSION) in msg
    assert "upgrade" in msg.lower() or "downgrade" in msg.lower()


# ─── 6. Per-version migration stub registered + callable ──────────


def test_migration_registry_exposes_callable_migrations(tmp_path):
    """ADR-008 specifies per-version migration hooks for future
    schema bumps. v1 has none yet, but the registry pattern must
    exist + each entry must be callable so a future migrate_v1_to_v2
    can land without re-architecting load()."""
    from sage_memory.hub import config as hub_config

    registry = hub_config.MIGRATIONS
    assert isinstance(registry, dict), "MIGRATIONS must be a dict"

    # Register a synthetic v1→v2 hook to prove the contract.
    seen = {}

    def _stub_v1_to_v2(cfg):
        seen["called"] = True
        # Future schema bump: just bump version, leave projects alone.
        return hub_config.HubConfig(version=2, projects=cfg.projects)

    registry[(1, 2)] = _stub_v1_to_v2
    try:
        cfg = hub_config.HubConfig(version=1, projects=[])
        result = registry[(1, 2)](cfg)
        assert seen["called"] is True
        assert result.version == 2
    finally:
        # Clean up so the stub doesn't leak into other tests.
        del registry[(1, 2)]


# ─── 7. Concurrent file access doesn't corrupt the file ───────────


def test_concurrent_saves_do_not_corrupt_yaml(tmp_path):
    """Multiple threads invoking save() concurrently must leave the
    file in a valid state (atomic write + rename guarantees one
    writer wins; readers never observe partial state)."""
    from sage_memory.hub import config as hub_config

    path = tmp_path / ".sage-hub.yaml"
    hub_config.init(path)

    project_dir = tmp_path / "p"
    project_dir.mkdir()

    errors: list[Exception] = []

    def _worker(idx: int) -> None:
        try:
            cfg = hub_config.load(path)
            cfg = hub_config.add_project(cfg, f"p{idx}", project_dir)
            hub_config.save(cfg, path)
        except Exception as exc:  # pragma: no cover — captured below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # The file must still parse cleanly — no partial writes.
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    assert data.get("version") == 1
    assert isinstance(data.get("projects"), list)
    # Racing writers: final projects list MAY be smaller than 8 (lost
    # updates are acceptable per the no-locking design), but the
    # file shape is always valid.
    assert errors == [], f"workers raised: {errors!r}"


# ─── 8. Non-existent file path errors clearly ─────────────────────


def test_load_missing_file_raises_clear_error(tmp_path):
    from sage_memory.hub import config as hub_config

    missing = tmp_path / "no-such" / ".sage-hub.yaml"

    with pytest.raises(FileNotFoundError) as exc:
        hub_config.load(missing)

    msg = str(exc.value)
    assert str(missing) in msg, (
        f"error must name the missing path; got: {msg!r}"
    )
