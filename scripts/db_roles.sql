\set ON_ERROR_STOP on
-- This file is intentionally outside /docker-entrypoint-initdb.d.
-- Only the migration container executes it with owner credentials.
SELECT format('CREATE ROLE %I LOGIN', :'app_user')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS',
  :'app_user', :'app_password'
) \gexec

-- The runtime principal must not own schema objects or inherit elevated grants.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";

-- Ordinary application work needs row reads/inserts/updates, not arbitrary deletion.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO :"app_user";
REVOKE DELETE ON ALL TABLES IN SCHEMA public FROM :"app_user";
GRANT DELETE ON TABLE public.user_warehouses, public.rate_limit_counters TO :"app_user";

-- The accounting and audit journals are insert-only for the application role.
REVOKE UPDATE, DELETE ON TABLE public.stock_movements, public.audit_events
  FROM :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";

-- New migrations are followed by this script, so new tables receive reviewed grants.
