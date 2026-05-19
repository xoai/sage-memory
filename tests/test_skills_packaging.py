"""Task 1 acceptance — bundled skills resolve via importlib.resources
and are present in both wheel and sdist.

Skills moved from repo-root `skills/` to `src/sage_memory/skills/` in
the 0.8.0 cycle so that `uvx sage-memory install-skills` works after
`pip install sage-memory` with no git clone. Hatchling's
`packages = ["src/sage_memory"]` includes them automatically; this
test asserts that claim against the built artifacts.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SKILLS = ("memory", "ontology", "self-learning")


# ---- Runtime resolution (no build required) ----

@pytest.mark.parametrize("skill", SKILLS)
def test_skill_md_resolves_at_runtime(skill: str):
    p = files("sage_memory") / "skills" / skill / "SKILL.md"
    assert p.is_file(), f"{p} not found"
    text = p.read_text()
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    assert "name:" in text[:200]


@pytest.mark.parametrize("skill", SKILLS)
def test_references_dir_resolves(skill: str):
    p = files("sage_memory") / "skills" / skill / "references"
    assert p.is_dir(), f"{p} not found"
    md_files = [x for x in p.iterdir() if x.name.endswith(".md")]
    assert md_files, f"no reference .md files under {p}"


def test_ontology_scripts_dir_resolves():
    p = files("sage_memory") / "skills" / "ontology" / "scripts"
    assert p.is_dir()
    names = {x.name for x in p.iterdir()}
    assert "graph_check.py" in names


# ---- Build-artifact verification ----
#
# These tests are slower (they invoke `uv build`). They're marked so a
# fast `pytest -m "not build"` skips them, but the full suite catches
# packaging regressions.

@pytest.fixture(scope="module")
def built_artifacts(tmp_path_factory):
    """Build wheel + sdist once per test session, return paths."""
    dist = REPO / "dist"
    # Clean dist to make this fixture deterministic.
    if dist.exists():
        for f in dist.glob("sage_memory-*"):
            f.unlink()
    result = subprocess.run(
        ["uv", "build"], cwd=REPO, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"uv build failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    wheels = sorted(dist.glob("sage_memory-*.whl"))
    sdists = sorted(dist.glob("sage_memory-*.tar.gz"))
    assert wheels, "uv build produced no wheel"
    assert sdists, "uv build produced no sdist"
    return {"wheel": wheels[-1], "sdist": sdists[-1]}


@pytest.mark.build
@pytest.mark.parametrize("skill", SKILLS)
def test_wheel_contains_skill(built_artifacts, skill: str):
    with zipfile.ZipFile(built_artifacts["wheel"]) as zf:
        names = zf.namelist()
    target = f"sage_memory/skills/{skill}/SKILL.md"
    assert any(n == target for n in names), (
        f"{target} missing from wheel; got skills entries: "
        f"{[n for n in names if 'skills' in n]}"
    )


@pytest.mark.build
@pytest.mark.parametrize("skill", SKILLS)
def test_sdist_contains_skill(built_artifacts, skill: str):
    with tarfile.open(built_artifacts["sdist"]) as tf:
        names = tf.getnames()
    target_suffix = f"/src/sage_memory/skills/{skill}/SKILL.md"
    assert any(n.endswith(target_suffix) for n in names), (
        f"{target_suffix} missing from sdist; "
        f"got skills entries: {[n for n in names if 'skills' in n]}"
    )


@pytest.mark.build
def test_sdist_extracts_with_resolvable_skills(built_artifacts, tmp_path):
    """The sdist must extract to a tree where importlib resolution
    would work after a fresh install (no broken symlinks, no missing
    files)."""
    with tarfile.open(built_artifacts["sdist"]) as tf:
        tf.extractall(tmp_path)
    extracted = next(tmp_path.glob("sage_memory-*"))
    for skill in SKILLS:
        skill_md = extracted / "src" / "sage_memory" / "skills" / skill / "SKILL.md"
        assert skill_md.is_file(), f"{skill_md} missing in extracted sdist"
        assert skill_md.read_text().startswith("---\n")
