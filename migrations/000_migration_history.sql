-- Migration 000: Migration history table
-- Tracks which migrations have been applied. Run this first.
-- Repeated execution is safe (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS schema_migrations (
  id SERIAL PRIMARY KEY,
  migration_id VARCHAR(10) NOT NULL UNIQUE,
  name TEXT NOT NULL,
  applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO schema_migrations (migration_id, name)
VALUES ('000', 'migration_history')
ON CONFLICT (migration_id) DO NOTHING;
