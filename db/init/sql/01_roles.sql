-- ---------------------------------------------------------------------------
-- Roles. Control C1 in docs/security-controls.md.
--
-- Two roles, never merged:
--   app_rw      (= POSTGRES_USER) owns everything and is used only by the service
--               for its own state: runs, traces, audit, LangGraph checkpoints.
--   analyst_ro  the ONLY role that agent-generated SQL ever runs under. It can read
--               schema analytics and nothing else, in read-only transactions, under a
--               statement timeout. Even a total validator bypass cannot write.
-- ---------------------------------------------------------------------------

-- Nothing may be created in public by anyone, including future roles.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"POSTGRES_DB" FROM PUBLIC;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user') THEN
        EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', :'ro_user', :'ro_password');
    ELSE
        EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L', :'ro_user', :'ro_password');
    END IF;
END
$$;

-- Read-only by construction: every transaction this role opens is read-only, whatever
-- the client sends. This is the layer that holds when all validation above it fails.
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;

-- Resource limits, control C5. A runaway query cannot pin the database.
ALTER ROLE :"ro_user" SET statement_timeout = :'statement_timeout';
ALTER ROLE :"ro_user" SET idle_in_transaction_session_timeout = :'idle_tx_timeout';

-- No temp objects, no large-object work, and a conservative work_mem.
ALTER ROLE :"ro_user" SET temp_file_limit = '256MB';
ALTER ROLE :"ro_user" SET work_mem = '32MB';
ALTER ROLE :"ro_user" SET lock_timeout = '5s';

-- The read-only role must never inherit membership that could grant write access.
ALTER ROLE :"ro_user" NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOREPLICATION NOBYPASSRLS;
