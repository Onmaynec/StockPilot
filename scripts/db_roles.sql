\set ON_ERROR_STOP on
-- This file is intentionally outside /docker-entrypoint-initdb.d.
-- Only the migration container executes it with owner credentials.
SELECT format('CREATE ROLE %I LOGIN', :'app_user')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password') \gexec
-- Never allow the application principal to own objects or run DDL.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
REVOKE UPDATE, DELETE ON TABLE public.stock_movements, public.audit_events
  FROM :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
-- Future migrations explicitly review new privileges and reapply this script.
