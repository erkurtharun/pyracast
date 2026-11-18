-- Applied by the PostgreSQL superuser in one transaction on every release and reset; every
-- statement converges, so a second run changes nothing.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pyracast_owner') THEN
    CREATE ROLE pyracast_owner;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pyracast_app') THEN
    CREATE ROLE pyracast_app;
  END IF;
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO pyracast_owner, pyracast_app', current_database());
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM pyracast_owner, pyracast_app', current_database());
END $$;
ALTER ROLE pyracast_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE pyracast_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT pyracast_owner TO CURRENT_USER WITH SET TRUE;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
ALTER SCHEMA public OWNER TO pyracast_owner;
SET LOCAL ROLE pyracast_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pyracast_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pyracast_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pyracast_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pyracast_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO pyracast_app;
-- Only migrations, run by the owner, write the ledger; default privileges granted it to the app.
DO $$
BEGIN
  IF to_regclass('public.django_migrations') IS NOT NULL THEN
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.django_migrations FROM pyracast_app;
  END IF;
END $$;
RESET ROLE;
