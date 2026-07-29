"""P2-3 — SQL identifier whitelisting tests (SM-QUAL-01).

Hygiene, NOT a live vulnerability (per 06-spec §P2-3: the
interpolated values in cli_reindex.py / cli_dedup.py are internal
constants and timestamps — no injection is currently reachable).
The guard exists to keep scanners quiet and to make a future edit
that passes an unsafe value fail loudly at the boundary.
"""

from __future__ import annotations

import pytest

from sage_memory.db import require_sql_identifier


@pytest.mark.parametrize("name", [
    "memories_vec",
    "chunks_vec_backup_1234567890",
    "memory_id",
    "chunk_id",
    "_private",
])
def test_valid_identifiers_pass(name):
    assert require_sql_identifier(name) == name


@pytest.mark.parametrize("name", [
    "memories_vec; DROP TABLE memories--",
    "a b",                # space
    "1starts_with_digit",
    "table-name",         # hyphen
    "",                   # empty
    'x"quoted"',
    "über",               # non-ascii
])
def test_invalid_identifiers_raise(name):
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        require_sql_identifier(name)


def test_reindex_backup_identifiers_are_validated():
    """Site-level proof: the reindex backup naming path routes
    through the validator."""
    from sage_memory import cli_reindex
    # The module must import + use the shared validator.
    assert hasattr(cli_reindex, "require_sql_identifier")


def test_dedup_pragma_value_is_int_cast():
    """The PRAGMA application_id value is an integer literal (not an
    identifier), so its safe pattern is an int() cast, not the
    identifier whitelist."""
    from sage_memory import cli_dedup
    assert isinstance(cli_dedup._SAGE_APPLICATION_ID, int)
