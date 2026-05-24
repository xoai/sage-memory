"""T5 — `sage-self-learning` skill extended with the
supersedes-for-paraphrase pattern (0.12.0+).

Verifies the new subsection lands without regressing the existing
"When a Learning Causes a Bug" section.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "sage_memory" / "skills" / "sage-self-learning" / "SKILL.md"
)


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_new_supersedes_subsection_present(skill_text):
    """The new subsection heading exists (case-insensitive
    substring; allows minor wording polish without breaking)."""
    lowered = skill_text.lower()
    # Heading mentions "paraphrase" + indicates the new 0.12.0+ behavior.
    assert "paraphrase" in lowered, (
        "self-learning SKILL must mention 'paraphrase' in the new "
        "supersedes subsection (0.12.0+ pattern)"
    )


def test_subsection_mentions_near_duplicate_confidence(skill_text):
    """The new pattern hinges on the `confidence: "near_duplicate"`
    signal from sage_memory_store responses."""
    assert "near_duplicate" in skill_text


def test_subsection_mentions_supersedes_relation_example(skill_text):
    """The pattern documents `relation="supersedes"` (or
    `relation: "supersedes"` — JSON/Python both acceptable)."""
    assert "supersedes" in skill_text


def test_existing_corrects_pattern_section_still_present(skill_text):
    """Regression guard: the existing 'When a Learning Causes a Bug'
    section + its `corrects` relation pattern must survive the edit."""
    assert "When a Learning Causes a Bug" in skill_text
    assert "corrects" in skill_text


def test_subsection_contrasts_supersedes_vs_corrects(skill_text):
    """The new subsection explicitly contrasts the two patterns so
    agents pick the right one. Heuristic: both `supersedes` and
    `corrects` appear within ~2000 chars of the word `paraphrase`."""
    idx = skill_text.lower().find("paraphrase")
    assert idx >= 0
    window = skill_text[max(0, idx - 200): idx + 2000].lower()
    assert "supersedes" in window
    assert "corrects" in window, (
        "new subsection should reference the existing `corrects` "
        "pattern so agents know when to use which"
    )
