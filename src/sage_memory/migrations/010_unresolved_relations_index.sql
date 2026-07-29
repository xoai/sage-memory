-- 010 — Partial index on unresolved code_relations target_name (P1-1).
--
-- Supports the incremental resolve pass: the DB-driven re-resolve
-- scans unresolved rows (target_symbol_id IS NULL), and agents query
-- unresolved relations by name. Partial index keeps it small —
-- resolved rows (the majority over time) are excluded.
--
-- Append-only per the migrations contract: never edit 001–009.

CREATE INDEX IF NOT EXISTS idx_code_relations_target_name_unresolved
    ON code_relations(target_name)
    WHERE target_symbol_id IS NULL;
