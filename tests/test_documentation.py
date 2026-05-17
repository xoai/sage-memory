"""M5 T6 — Documentation content-bar tests.

Per spec A13 (rev1-review Minor #10 tightened bar): README, CHANGELOG,
and config.yaml.example must exist AND meet specific content checks.
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_readme_retrieval_pipeline_section_meets_bar():
    """README has a 'Retrieval Pipeline' section ≥250 words; cites
    ADR-001/003/004/005; mentions all 3 channels + 6 stages."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Retrieval Pipeline" in readme, (
        "README must have a '## Retrieval Pipeline' section"
    )

    # Extract the section body
    sections = readme.split("## Retrieval Pipeline", 1)
    assert len(sections) == 2
    after = sections[1]
    # End at the next H2 heading
    body = after.split("\n## ", 1)[0]

    # Word count ≥250
    words = len(body.split())
    assert words >= 250, (
        f"Retrieval Pipeline section must be ≥250 words; got {words}"
    )

    # Cite 4 ADRs by ID
    for adr in ("ADR-001", "ADR-003", "ADR-004", "ADR-005"):
        assert adr in body, f"section must cite {adr} by ID"

    # All 3 channels named
    for ch in ("bm25", "vector", "graph"):
        assert ch in body.lower(), f"section must mention {ch} channel"

    # All 6 pipeline stages named
    for stage in ("expand", "retrieve", "fuse", "dedup", "rerank", "score"):
        assert stage in body.lower(), (
            f"section must name pipeline stage: {stage}"
        )


def test_changelog_per_milestone_entries():
    """CHANGELOG has a 0.6.0 release header with one entry per
    shipped milestone: M1, M2, M3a, M3b, M4, M5."""
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "0.6.0" in changelog, "CHANGELOG must have a 0.6.0 entry"

    sections = changelog.split("## [0.6.0]", 1)
    assert len(sections) == 2
    body = sections[1].split("\n## ", 1)[0]
    for milestone in ("M1", "M2", "M3a", "M3b", "M4", "M5"):
        assert f"### {milestone}" in body, (
            f"0.6.0 section must have '### {milestone}' entry"
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
