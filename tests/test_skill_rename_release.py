"""Task 7 — 0.10.0 release acceptance.

Version, CHANGELOG, README assertions for the skill-rename cycle.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_pyproject_version_is_0_10_0():
    text = (REPO / "pyproject.toml").read_text()
    assert 'version = "0.10.0"' in text


def test_package_version_resolves_to_0_10_0():
    import sage_memory
    assert sage_memory.__version__ == "0.10.0"


def test_changelog_has_0_10_0_entry_with_migration_callouts():
    text = (REPO / "CHANGELOG.md").read_text()
    assert "## [0.10.0]" in text
    section = text.split("## [0.10.0]", 1)[1].split("\n## ", 1)[0]
    # All 5 spec-mandated callouts present
    assert "Source folders renamed" in section or "Source folders" in section
    assert "Marker-block migration" in section
    assert "install paths" in section.lower() and "byte-identical" in section
    assert "Breaking" in section
    assert "Aesthetic" in section or "marker blocks now read" in section.lower()
    # Migration target version referenced
    assert "0.10.0" in section


def test_readme_mentions_sage_prefix_in_skills_section():
    text = (REPO / "README.md").read_text()
    # Note about the rename in the Skills section
    assert "Skill identifiers are prefixed" in text or (
        "sage-" in text and "0.10.0" in text and "skill" in text.lower()
    )
    # Three skill section headers updated
    assert "### sage-memory → Knowledge" in text
    assert "### sage-ontology → Structure" in text
    assert "### sage-self-learning → Experience" in text
