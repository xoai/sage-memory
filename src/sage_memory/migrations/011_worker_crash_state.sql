-- 011 — worker_state crash observability (P1-3, SM-REL-01).
--
-- The background worker previously died silently on startup failures
-- (e.g. querying extraction_queue on an unmigrated DB). Worker.run
-- now records top-level crashes here so `sage-memory worker --status`
-- can surface "crashed: <reason>" instead of silence.
--
-- Append-only per the migrations contract: ALTER TABLE ADD COLUMN,
-- never edit 001–010.

ALTER TABLE worker_state ADD COLUMN last_error TEXT;
ALTER TABLE worker_state ADD COLUMN last_error_at REAL;
