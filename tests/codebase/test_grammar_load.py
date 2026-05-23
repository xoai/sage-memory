"""T1 — grammar-load smoke test (rev 3 Major #5 fix).

Verifies each of the 10 languages we ship queries for has a working
grammar in the installed ``tree-sitter-language-pack``. Catches pack
version drift loudly (one parametrize row fails) instead of cryptically
(per-language extraction tests fail with "no symbols extracted").

The whole module is skipped when the ``[codebase]`` extra is not
installed via the module-level ``pytest.importorskip``.
"""

from __future__ import annotations

import pytest

tsp = pytest.importorskip("tree_sitter_language_pack")


# 11 entries: typescript + tsx are separate grammars (the pack ships
# both; tsx parses JSX, typescript does not).
GRAMMARS = [
    "python",
    "typescript",
    "tsx",
    "javascript",
    "go",
    "rust",
    "java",
    "ruby",
    "php",
    "c",
    "cpp",
]


@pytest.mark.parametrize("name", GRAMMARS)
def test_grammar_loads(name: str):
    parser = tsp.get_parser(name)
    assert parser is not None
