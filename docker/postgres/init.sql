-- Recreates the production permission model locally.
-- Prisma owns `public`. Django owns `consumer` and may only read `public`.

CREATE SCHEMA IF NOT EXISTS consumer;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'consumer_app') THEN
    CREATE ROLE consumer_app LOGIN PASSWORD 'consumer';
  END IF;
END
$$;

-- Full ownership of the consumer schema.
ALTER SCHEMA consumer OWNER TO consumer_app;
GRANT ALL ON SCHEMA consumer TO consumer_app;

-- Read-only on public, and explicitly NOT able to create there.
GRANT USAGE ON SCHEMA public TO consumer_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO consumer_app;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO consumer_app;
REVOKE CREATE ON SCHEMA public FROM consumer_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Tables Prisma creates later are covered automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO consumer_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO consumer_app;