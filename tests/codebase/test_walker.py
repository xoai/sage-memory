"""T3 — filesystem walker tests.

Covers the extension map (10 languages), the ``.h`` C-vs-C++ heuristic
(6 cases incl. cross-OS sorted determinism), ``git ls-files`` gitignore
integration with deterministic fallback paths, ``SKIP_DIRS`` enforcement,
unknown-extension silence, the ``--include-ignored`` bypass and the
``languages=`` filter.

These tests do NOT require the ``[codebase]`` extra — the walker has no
tree-sitter dependency. Git is required for the integration cases; we
skip those when git is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sage_memory.codebase import _languages, _walker
from sage_memory.codebase._languages import (
    EXT_MAP,
    resolve_h_file,
)
from sage_memory.codebase._walker import SKIP_DIRS, walk


# ---------------------------------------------------------------------------
# Extension map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ext,expected",
    [
        (".py", ("py", "python", "py")),
        (".ts", ("ts", "typescript", "ts")),
        (".tsx", ("ts", "tsx", "tsx")),
        (".js", ("js", "javascript", "js")),
        (".mjs", ("js", "javascript", "js")),
        (".cjs", ("js", "javascript", "js")),
        (".jsx", ("js", "tsx", "js")),
        (".go", ("go", "go", "go")),
        (".rs", ("rs", "rust", "rs")),
        (".java", ("java", "java", "java")),
        (".rb", ("rb", "ruby", "rb")),
        (".php", ("php", "php", "php")),
        (".c", ("c", "c", "c")),
        (".cpp", ("cpp", "cpp", "cpp")),
        (".cc", ("cpp", "cpp", "cpp")),
        (".cxx", ("cpp", "cpp", "cpp")),
        (".hpp", ("cpp", "cpp", "cpp")),
        (".hxx", ("cpp", "cpp", "cpp")),
    ],
)
def test_ext_map_entries(ext: str, expected: tuple[str, str, str]) -> None:
    assert EXT_MAP[ext] == expected


def test_ext_map_does_not_contain_h() -> None:
    # .h is resolved dynamically by resolve_h_file (heuristic), not by
    # static EXT_MAP lookup. Keeping it out of the map prevents callers
    # from accidentally bypassing the heuristic.
    assert ".h" not in EXT_MAP


# ---------------------------------------------------------------------------
# `.h` ambiguity heuristic — (a)..(f) per plan rev 3
# ---------------------------------------------------------------------------


def test_h_heuristic_a_pure_h_dir(tmp_path: Path) -> None:
    """(a) Directory with only `.h` files → c."""
    (tmp_path / "foo.h").write_text("")
    (tmp_path / "bar.h").write_text("")
    assert resolve_h_file(tmp_path) == ("c", "c", "c")


def test_h_heuristic_b_h_plus_cpp(tmp_path: Path) -> None:
    """(b) `.h` + `.cpp` → cpp."""
    (tmp_path / "foo.h").write_text("")
    (tmp_path / "foo.cpp").write_text("")
    assert resolve_h_file(tmp_path) == ("cpp", "cpp", "cpp")


def test_h_heuristic_c_h_plus_c(tmp_path: Path) -> None:
    """(c) `.h` + `.c` (no cpp-evidence) → c."""
    (tmp_path / "foo.h").write_text("")
    (tmp_path / "foo.c").write_text("")
    assert resolve_h_file(tmp_path) == ("c", "c", "c")


def test_h_heuristic_d_two_h_one_cpp_walker_cache(tmp_path: Path) -> None:
    """(d) Two `.h` + one `.cpp` in same dir → walker yields all three
    files; both `.h` resolve to cpp via the per-dir cache.
    """
    (tmp_path / "a.h").write_text("")
    (tmp_path / "b.h").write_text("")
    (tmp_path / "impl.cpp").write_text("")
    yielded = {rel.name: triple for rel, *triple_rest in walk(tmp_path, include_ignored=True)
               for triple in [tuple(triple_rest)]}
    assert yielded["a.h"] == ("cpp", "cpp", "cpp")
    assert yielded["b.h"] == ("cpp", "cpp", "cpp")
    assert yielded["impl.cpp"] == ("cpp", "cpp", "cpp")


def test_h_heuristic_e_h_plus_hpp_no_cpp(tmp_path: Path) -> None:
    """(e) `.h` + `.hpp` (no `.cpp`) → cpp. Rev 3 added `.hpp`/`.hxx`
    to the cpp-evidence set; `.hpp` only exists in C++ practice.
    """
    (tmp_path / "foo.h").write_text("")
    (tmp_path / "foo.hpp").write_text("")
    assert resolve_h_file(tmp_path) == ("cpp", "cpp", "cpp")


def test_h_heuristic_e2_h_plus_hxx_no_cpp(tmp_path: Path) -> None:
    """(e') Same as (e) but with `.hxx` — also rev 3 expansion."""
    (tmp_path / "foo.h").write_text("")
    (tmp_path / "foo.hxx").write_text("")
    assert resolve_h_file(tmp_path) == ("cpp", "cpp", "cpp")


def test_h_heuristic_f_sorted_walk_order(tmp_path: Path) -> None:
    """(f) Walker yields filenames in sorted order so the per-dir cache
    populates deterministically across OSes / filesystems.
    """
    for name in ["z.h", "a.h", "m.cpp", "b.cpp", "y.h"]:
        (tmp_path / name).write_text("")
    rels = [str(rel) for rel, *_ in walk(tmp_path, include_ignored=True)]
    assert rels == sorted(rels)


# ---------------------------------------------------------------------------
# Walker — basic shape
# ---------------------------------------------------------------------------


def test_walk_yields_4_tuple(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("def f(): pass\n")
    results = list(walk(tmp_path, include_ignored=True))
    assert len(results) == 1
    rel, language_tag, grammar_name, query_id = results[0]
    assert isinstance(rel, Path)
    assert str(rel) == "hello.py"
    assert (language_tag, grammar_name, query_id) == ("py", "python", "py")


def test_walk_unknown_extension_silently_skipped(tmp_path: Path) -> None:
    (tmp_path / "data.foo").write_text("x")
    (tmp_path / "README.md").write_text("# hi")
    (tmp_path / "main.py").write_text("")
    rels = [str(rel) for rel, *_ in walk(tmp_path, include_ignored=True)]
    assert rels == ["main.py"]


def test_walk_skip_dirs_pruned(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "foo.py").write_text("")
    rels = [str(rel) for rel, *_ in walk(tmp_path, include_ignored=True)]
    assert rels == ["main.py"]


def test_walk_languages_filter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.ts").write_text("")
    (tmp_path / "c.go").write_text("")
    rels = sorted(
        str(rel) for rel, *_ in walk(tmp_path, languages=["py", "go"], include_ignored=True)
    )
    assert rels == ["a.py", "c.go"]


def test_walk_recurses_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "b.py").write_text("")
    (sub / "c.py").write_text("")
    rels = sorted(str(rel) for rel, *_ in walk(tmp_path, include_ignored=True))
    # Paths use OS separator; normalize for cross-platform comparison.
    rels_posix = [Path(r).as_posix() for r in rels]
    assert rels_posix == ["a.py", "pkg/b.py", "pkg/c.py"]


# ---------------------------------------------------------------------------
# `.h` heuristic — integration through the walker
# ---------------------------------------------------------------------------


def test_walk_h_file_classified_as_cpp_when_cpp_sibling(tmp_path: Path) -> None:
    (tmp_path / "lib.h").write_text("")
    (tmp_path / "lib.cpp").write_text("")
    out = {rel.name: (lt, gn, qi) for rel, lt, gn, qi in walk(tmp_path, include_ignored=True)}
    assert out["lib.h"] == ("cpp", "cpp", "cpp")
    assert out["lib.cpp"] == ("cpp", "cpp", "cpp")


def test_walk_h_file_classified_as_c_when_alone(tmp_path: Path) -> None:
    (tmp_path / "lib.h").write_text("")
    (tmp_path / "lib.c").write_text("")
    out = {rel.name: (lt, gn, qi) for rel, lt, gn, qi in walk(tmp_path, include_ignored=True)}
    assert out["lib.h"] == ("c", "c", "c")
    assert out["lib.c"] == ("c", "c", "c")


# ---------------------------------------------------------------------------
# Gitignore integration (real git)
# ---------------------------------------------------------------------------


_git_required = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


def _init_git(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    # Required on some hosts where `git init` triggers identity checks
    # when subsequent commands run.
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@_git_required
def test_walk_respects_gitignore(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / ".gitignore").write_text("secret.py\nbuild_artifacts/\n")
    (tmp_path / "main.py").write_text("")
    (tmp_path / "secret.py").write_text("")
    (tmp_path / "build_artifacts").mkdir()
    (tmp_path / "build_artifacts" / "out.py").write_text("")

    rels = sorted(str(rel) for rel, *_ in walk(tmp_path, include_ignored=False))
    rels_posix = [Path(r).as_posix() for r in rels]
    assert "main.py" in rels_posix
    assert "secret.py" not in rels_posix
    assert all(not r.startswith("build_artifacts/") for r in rels_posix)


@_git_required
def test_walk_include_ignored_bypasses_gitignore_but_keeps_skip_dirs(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / ".gitignore").write_text("secret.py\n")
    (tmp_path / "main.py").write_text("")
    (tmp_path / "secret.py").write_text("")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "leaf.js").write_text("")

    rels = sorted(str(rel) for rel, *_ in walk(tmp_path, include_ignored=True))
    rels_posix = [Path(r).as_posix() for r in rels]
    # Gitignore bypass: secret.py now visible.
    assert "main.py" in rels_posix
    assert "secret.py" in rels_posix
    # SKIP_DIRS still applies.
    assert all(not r.startswith("node_modules") for r in rels_posix)


# ---------------------------------------------------------------------------
# Gitignore fallback — git missing, timeout
# ---------------------------------------------------------------------------


def test_walk_falls_back_when_not_a_git_repo(tmp_path: Path) -> None:
    """No .git/ anywhere → walker skips git ls-files, applies SKIP_DIRS only.
    All known-extension files in non-skipped dirs are yielded.
    """
    (tmp_path / "main.py").write_text("")
    (tmp_path / "secret.py").write_text("")  # would be ignored if git were used
    rels = sorted(str(rel) for rel, *_ in walk(tmp_path, include_ignored=False))
    rels_posix = [Path(r).as_posix() for r in rels]
    assert "main.py" in rels_posix
    assert "secret.py" in rels_posix


def test_walk_falls_back_when_git_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.run raises FileNotFoundError → fall back to SKIP_DIRS only."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(_walker.subprocess, "run", fake_run)
    (tmp_path / "main.py").write_text("")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "leaf.js").write_text("")

    rels = sorted(str(rel) for rel, *_ in walk(tmp_path, include_ignored=False))
    rels_posix = [Path(r).as_posix() for r in rels]
    assert "main.py" in rels_posix
    # SKIP_DIRS still honored under fallback.
    assert all(not r.startswith("node_modules") for r in rels_posix)


def test_walk_falls_back_when_git_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(_walker.subprocess, "run", fake_run)
    (tmp_path / "main.py").write_text("")
    rels = [str(rel) for rel, *_ in walk(tmp_path, include_ignored=False)]
    assert rels == ["main.py"]


def test_walk_falls_back_when_git_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git ls-files exits non-zero (e.g., malformed .gitignore) → fall back."""

    class _Result:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad .gitignore"

    def fake_run(*args, **kwargs):
        return _Result()

    monkeypatch.setattr(_walker.subprocess, "run", fake_run)
    (tmp_path / "main.py").write_text("")
    rels = [str(rel) for rel, *_ in walk(tmp_path, include_ignored=False)]
    assert rels == ["main.py"]


# ---------------------------------------------------------------------------
# Skip-list contents — sanity guard against regressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".git", ".hg", ".svn",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "node_modules", "bower_components",
        "dist", "build", "target", "vendor",
        ".venv", ".tox", "venv", "env",
        ".gradle", ".idea", ".vscode",
        "obj", "bin",
    ],
)
def test_skip_dirs_contains(name: str) -> None:
    assert name in SKIP_DIRS
