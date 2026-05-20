"""Task 9 — Skill text checks for the 0.9.0 agent-driven extraction
narrative.

Each bundled SKILL.md must:
  - mention the new `entities` / `relations` params
  - include a concrete code example using them
  - reference the agent-driven extraction model (not the worker
    extraction model) as the primary path
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILLS_ROOT = (
    Path(__file__).resolve().parent.parent
    / "src" / "sage_memory" / "skills"
)


@pytest.mark.parametrize("skill", ["sage-memory", "sage-ontology", "sage-self-learning"])
def test_skill_md_references_entities_param(skill):
    """The new `entities` parameter must be visible in each skill's
    SKILL.md — either via a code example or in prose."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text()
    assert "entities:" in text or "entities=[" in text, (
        f"{skill}/SKILL.md must reference the `entities` param "
        f"introduced in 0.9.0"
    )


def test_memory_skill_has_extract_before_store_section():
    text = (SKILLS_ROOT / "sage-memory" / "SKILL.md").read_text()
    assert "### Extract Before Store" in text
    # Has the controlled vocab callout
    assert "PERSON, CONCEPT, TECHNOLOGY" in text or "TECHNOLOGY" in text


def test_memory_skill_mentions_suggested_links():
    text = (SKILLS_ROOT / "sage-memory" / "SKILL.md").read_text()
    assert "suggested_links" in text


def test_ontology_skill_mentions_agent_driven_extraction():
    text = (SKILLS_ROOT / "sage-ontology" / "SKILL.md").read_text()
    assert "Agent-driven extraction" in text or "agent-driven" in text.lower()


def test_self_learning_skill_extract_pattern_in_how_to_store():
    text = (SKILLS_ROOT / "sage-self-learning" / "SKILL.md").read_text()
    assert "Extract Before Store" in text
    # Self-learning specifically links Prevention to entities
    assert "Prevention" in text and "entity" in text.lower()


@pytest.mark.parametrize("skill", ["sage-memory", "sage-ontology", "sage-self-learning"])
def test_skill_md_remains_valid_yaml_frontmatter(skill):
    """Quick sanity check: each SKILL.md still starts with a YAML
    frontmatter block."""
    text = (SKILLS_ROOT / skill / "SKILL.md").read_text()
    assert text.startswith("---\n"), (
        f"{skill}/SKILL.md must start with YAML frontmatter"
    )
    # Frontmatter closes
    assert text.count("\n---\n") >= 1
