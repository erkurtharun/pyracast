-- Run once as the PostgreSQL administrator in the pyracast database.
BEGIN;
CREATE ROLE pyracast_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE pyracast_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT pyracast_owner TO CURRENT_USER WITH SET TRUE;
REVOKE ALL ON DATABASE pyracast FROM PUBLIC;
GRANT CONNECT ON DATABASE pyracast TO pyracast_owner, pyracast_app;
-- PostgreSQL requires database CREATE and SET ROLE for a non-superuser ownership transfer.
GRANT CREATE ON DATABASE pyracast TO pyracast_owner;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
ALTER SCHEMA public OWNER TO pyracast_owner;
SET ROLE pyracast_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pyracast_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pyracast_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pyracast_app;
ALTER DEFAULT PRIVILEGES FOR ROLE pyracast_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO pyracast_app;
RESET ROLE;
REVOKE CREATE ON DATABASE pyracast FROM pyracast_owner;
COMMIT;
