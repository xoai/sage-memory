-- sage-memory schema v3: memory health
-- Adds status lifecycle for invalidation/archival.
-- Access tracking (accessed_at, access_count) already exists from v1.

ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active';
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
