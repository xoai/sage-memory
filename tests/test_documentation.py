"""M5 T6 — Documentation content-bar tests.

Per spec A13 (rev1-review Minor #10 tightened bar): README, CHANGELOG,
and config.yaml.example must exist AND meet specific content checks.
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_retrieval_pipeline_section_meets_bar():
    """README has a 'Retrieval Pipeline' section ≥250 words; mentions
    all 3 channels + 6 stages. (ADR citations removed in 0.7.0 docs
    cleanup — ADRs live in gitignored .sage/docs/.)"""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Retrieval Pipeline" in readme, (
        "README must have a '## Retrieval Pipeline' section"
    )

    sections = readme.split("## Retrieval Pipeline", 1)
    assert len(sections) == 2
    after = sections[1]
    body = after.split("\n## ", 1)[0]

    words = len(body.split())
    assert words >= 250, (
        f"Retrieval Pipeline section must be ≥250 words; got {words}"
    )

    for ch in ("bm25", "vector", "graph"):
        assert ch in body.lower(), f"section must mention {ch} channel"

    for stage in ("expand", "retrieve", "fuse", "dedup", "rerank", "score"):
        assert stage in body.lower(), (
            f"section must name pipeline stage: {stage}"
        )


def test_changelog_06x_07x_entries_present():
    """CHANGELOG has 0.6.0 and 0.7.0 entries with substantive content.
    Replaces the M1-M5 sub-header check (those headers were removed in
    the 0.7.0 changelog rewrite per user directive)."""
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.6.0]" in changelog, "CHANGELOG must have a 0.6.0 entry"
    assert "## [0.7.0]" in changelog, "CHANGELOG must have a 0.7.0 entry"

    # Each release section should have at least an Added or Fixed group
    for version in ("0.6.0", "0.7.0"):
        section = changelog.split(f"## [{version}]", 1)[1]
        body = section.split("\n## ", 1)[0]
        has_group = any(h in body for h in ("### Added", "### Fixed", "### Changed", "### Removed"))
        assert has_group, (
            f"{version} section must have at least one ### group "
            f"(Added/Fixed/Changed/Removed)"
        )


def test_config_yaml_example_covers_required_keys():
    """`docs/config.yaml.example` exists; ≥30 non-empty lines;
    covers required key paths from spec A13.

    P0-1b (SM-BUG-01): the example previously lived at
    `.sage/config.yaml.example`, but `.gitignore` ignores `.sage/` —
    the file could never be committed and this test could never pass
    on a clean clone. Moved to `docs/` (option A): `.sage/` stays
    purely runtime state.
    """
    path = _REPO_ROOT / "docs/config.yaml.example"
    assert path.exists(), "docs/config.yaml.example must exist"
    lines = [
        line for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 30, (
        f"docs/config.yaml.example must be ≥30 non-empty lines; "
        f"got {len(lines)}"
    )

    content = path.read_text("utf-8")
    for key_path in (
        "sage_memory:",
        "retrieval:",
        "expand:",
        "rerank:",
        "channels:",
        "fusion:",
        "dedup:",
        "interval:",
        "llm:",
        "embedding:",
    ):
        assert key_path in content, (
            f"config.yaml.example must include {key_path!r}"
        )


def test_config_yaml_example_keys_are_recognised():
    """Reverse-drift guard (P0-1b): every concrete key in the example
    must be a recognised config key — i.e. resolvable through
    ``config.get()`` against the built-in defaults tree.

    The forward direction (test above) proves required keys are
    present; this proves no stale/typo'd keys crept in. The example
    top-level `sage_memory:` namespace is stripped before resolution,
    matching ``config.py:_load_yaml``.
    """
    import yaml

    from sage_memory import config as cfg

    raw = (_REPO_ROOT / "docs/config.yaml.example").read_text("utf-8")
    parsed = yaml.safe_load(raw)
    tree = parsed.get("sage_memory", {})
    assert isinstance(tree, dict) and tree, (
        "example must have a non-empty sage_memory mapping"
    )

    def _walk(node: dict, prefix: str) -> list[str]:
        paths = []
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and value:
                paths.extend(_walk(value, dotted))
            elif not isinstance(value, dict):  # leaf
                paths.append(dotted)
            # empty dict {} = documented-but-env-only section; skip
        return paths

    for dotted in _walk(tree, ""):
        try:
            cfg.get(dotted)
        except cfg.ConfigError as e:
            raise AssertionError(
                f"example key {dotted!r} is not a recognised config "
                f"key: {e}"
            ) from e
