"""Per-language configuration for the codebase scanner.

``EXT_MAP`` maps every unambiguous file extension to a
``(language_tag, grammar_name, query_id)`` triple. The triple separates
three concerns:

- ``language_tag`` — stored in ``code_symbols.language`` (one of:
  ``py | ts | js | go | rs | java | rb | php | c | cpp``).
- ``grammar_name`` — string passed to
  ``tree_sitter_language_pack.get_parser(...)``. TypeScript and TSX use
  distinct grammars; ``.jsx`` is parsed by the ``tsx`` grammar because
  the pack's ``javascript`` grammar does not handle JSX syntax.
- ``query_id`` — the ``.scm`` query file under
  ``sage_memory.codebase.queries``. ``.tsx`` uses ``tsx.scm``,
  ``.jsx`` uses ``js.scm`` (it's still JavaScript semantically).

``.h`` is intentionally absent from ``EXT_MAP`` — it requires the
:func:`resolve_h_file` heuristic. Keeping it out of the static table
prevents accidental bypass.
"""

from __future__ import annotations

from pathlib import Path


EXT_MAP: dict[str, tuple[str, str, str]] = {
    ".py": ("py", "python", "py"),
    ".ts": ("ts", "typescript", "ts"),
    ".tsx": ("ts", "tsx", "tsx"),
    ".js": ("js", "javascript", "js"),
    ".mjs": ("js", "javascript", "js"),
    ".cjs": ("js", "javascript", "js"),
    ".jsx": ("js", "tsx", "js"),
    ".go": ("go", "go", "go"),
    ".rs": ("rs", "rust", "rs"),
    ".java": ("java", "java", "java"),
    ".rb": ("rb", "ruby", "rb"),
    ".php": ("php", "php", "php"),
    ".c": ("c", "c", "c"),
    ".cpp": ("cpp", "cpp", "cpp"),
    ".cc": ("cpp", "cpp", "cpp"),
    ".cxx": ("cpp", "cpp", "cpp"),
    ".hpp": ("cpp", "cpp", "cpp"),
    ".hxx": ("cpp", "cpp", "cpp"),
}


# Human-readable language label for use in file-memory ``content``
# strings (spec line 143: ``Source file (Python) — ...``). Indexed by
# the ``language_tag`` (first element of an ``EXT_MAP`` triple).
LANGUAGE_LABELS: dict[str, str] = {
    "py": "Python",
    "ts": "TypeScript",
    "js": "JavaScript",
    "go": "Go",
    "rs": "Rust",
    "java": "Java",
    "rb": "Ruby",
    "php": "PHP",
    "c": "C",
    "cpp": "C++",
}


# Sibling extensions whose presence in a directory means any `.h` in
# that directory is C++. `.hpp`/`.hxx` are included (rev 3) because they
# only exist in C++ practice — strong evidence.
_CPP_EVIDENCE_EXTS = frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hxx"})

_C_TRIPLE: tuple[str, str, str] = ("c", "c", "c")
_CPP_TRIPLE: tuple[str, str, str] = ("cpp", "cpp", "cpp")


def resolve_h_file(dir_path: Path) -> tuple[str, str, str]:
    """Classify a ``.h`` file in ``dir_path`` as C or C++.

    Default: C. Promoted to C++ when any sibling matches
    ``_CPP_EVIDENCE_EXTS``. The directory listing is iterated in sorted
    order so the result is deterministic across filesystems (some report
    entries in inode order, others alphabetically).
    """
    try:
        names = sorted(p.name for p in dir_path.iterdir() if p.is_file())
    except OSError:
        return _C_TRIPLE
    for name in names:
        if Path(name).suffix.lower() in _CPP_EVIDENCE_EXTS:
            return _CPP_TRIPLE
    return _C_TRIPLE
