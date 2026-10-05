-- Run on every `docker compose up` by the `db-setup` service.
-- Creates the analytics database only if it does not exist yet, so it works
-- for brand-new and existing Postgres volumes alike. No manual step needed.
SELECT 'CREATE DATABASE archisynapse_analytics'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'archisynapse_analytics')
\gexec
