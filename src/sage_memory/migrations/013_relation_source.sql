-- 013 — code_relations provenance column (P2-4, SM-CAP-01 adjacent).
--
-- Cross-tool graph import needs native-vs-imported edges to be
-- distinguishable in every query. 'native' DEFAULT means the existing
-- scan/resolve path writes nothing new — only the importer sets
-- 'import:<tool>'.
--
-- Append-only per the migrations contract: ALTER TABLE ADD COLUMN,
-- never edit 001–012.

ALTER TABLE code_relations
    ADD COLUMN source TEXT NOT NULL DEFAULT 'native';
