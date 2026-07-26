-- The app sets search_path to the `consumer` schema (see config/settings/base.py).
-- Postgres will not auto-create it, so ensure it exists before migrations run.
CREATE SCHEMA IF NOT EXISTS consumer;
