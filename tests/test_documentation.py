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
    """`.sage/config.yaml.example` exists; ≥30 non-empty lines;
    covers required key paths from spec A13."""
    path = _REPO_ROOT / ".sage/config.yaml.example"
    assert path.exists(), ".sage/config.yaml.example must exist"
    lines = [
        line for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 30, (
        f".sage/config.yaml.example must be ≥30 non-empty lines; "
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
