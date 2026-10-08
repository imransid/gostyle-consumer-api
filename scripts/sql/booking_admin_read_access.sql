-- Lets customer-api's Django admin READ booking-api's tables
-- (apps/booking_data). SELECT only: consumer_app cannot write there.
--
-- Run it ONCE on the gostyle_postgres server, as postgres, connected to the
-- BOOKING database, for example:
--
--   docker exec -i $(docker ps -qf name=gostyle_postgres) \
--     psql -U postgres -d gostyle_booking -v ON_ERROR_STOP=1 \
--     < booking_admin_read_access.sql
--
-- Then set BOOKING_DB_NAME=gostyle_booking in customer-api's stack.env and
-- redeploy. The admin reuses customer-api's own login (DB_USER, DB_PASSWORD).
--
-- Safe to run again. To undo:
--   REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM consumer_app;
--   REVOKE USAGE ON SCHEMA public FROM consumer_app;
--   REVOKE CONNECT ON DATABASE gostyle_booking FROM consumer_app;
--   (and the ALTER DEFAULT PRIVILEGES below with REVOKE instead of GRANT)

SELECT current_database() = 'gostyle_booking' AS on_booking_db \gset
\if :on_booking_db
\else
  \echo 'Connect to gostyle_booking, not' :DBNAME
  \quit
\endif

GRANT CONNECT ON DATABASE gostyle_booking TO consumer_app;
GRANT USAGE ON SCHEMA public TO consumer_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO consumer_app;

-- Tables booking-api adds later. Default privileges belong to the role that
-- creates the tables, so this names the owner of the existing ones: the role
-- booking-api migrates as.
SELECT tableowner AS booking_owner FROM pg_tables
 WHERE schemaname = 'public' AND tablename = 'booking' \gset
ALTER DEFAULT PRIVILEGES FOR ROLE :"booking_owner" IN SCHEMA public
  GRANT SELECT ON TABLES TO consumer_app;

-- The answer: every table should say true.
SELECT table_name,
       has_table_privilege('consumer_app', 'public.' || quote_ident(table_name), 'SELECT') AS can_read
  FROM information_schema.tables
 WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
 ORDER BY 1;
