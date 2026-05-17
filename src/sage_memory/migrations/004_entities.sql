-- Migration 004: Entities, mentions, relations (M1 / ADR-001)
-- Auto-extracted entity graph. Tables stay empty until M3a's worker
-- runs entity extraction.

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,  -- lowercase, punctuation stripped
    type            TEXT NOT NULL,  -- PERSON|CONCEPT|TECHNOLOGY|PROJECT|EVENT|OTHER
    canonical_id    TEXT REFERENCES entities(id) ON DELETE SET NULL,
    mention_count   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE (name_normalized, type)
);

CREATE TABLE IF NOT EXISTS mentions (
    memory_id     TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    entity_id     TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    surface_form  TEXT NOT NULL,
    context_start INTEGER,
    context_end   INTEGER,
    confidence    REAL NOT NULL DEFAULT 1.0,
    created_at    REAL NOT NULL,
    PRIMARY KEY (memory_id, entity_id, surface_form)
);

CREATE TABLE IF NOT EXISTS relations (
    id                TEXT PRIMARY KEY,
    source_entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id  TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation_type     TEXT NOT NULL,
    source_memory_id  TEXT REFERENCES memories(id) ON DELETE SET NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    created_at        REAL NOT NULL,
    UNIQUE (source_entity_id, target_entity_id, relation_type, source_memory_id)
);

CREATE INDEX IF NOT EXISTS idx_entities_name_norm ON entities(name_normalized);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_id)
    WHERE canonical_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_tgt ON relations(target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_src_mem ON relations(source_memory_id)
    WHERE source_memory_id IS NOT NULL;
