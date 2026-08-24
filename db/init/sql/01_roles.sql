-- ---------------------------------------------------------------------------
-- Roles. Control C1 in docs/security-controls.md.
--
-- Two roles, never merged:
--   app_rw      (= POSTGRES_USER) owns everything and is used only by the service for its
--               own state: runs, traces, audit, LangGraph checkpoints.
--   analyst_ro  the ONLY role that agent-generated SQL ever runs under. It can read schema
--               analytics and nothing else, in read-only transactions, under a statement
--               timeout. Even a total validator bypass cannot write.
--
-- Note on psql variables: substitution does not happen inside dollar-quoted strings, so no
-- DO $$ ... $$ blocks are used here. :"name" interpolates a quoted identifier and :'name' a
-- quoted literal. This file runs once, on first creation of the cluster.
-- ---------------------------------------------------------------------------

-- Nothing may be created in public by anyone, including roles added later.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;

CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_password';

-- Read-only by construction: every transaction this role opens is read-only, whatever the
-- client sends. This is the layer that holds when everything above it fails.
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;

-- Resource limits, control C5. A runaway query cannot pin the database.
ALTER ROLE :"ro_user" SET statement_timeout = :'statement_timeout';
ALTER ROLE :"ro_user" SET idle_in_transaction_session_timeout = :'idle_tx_timeout';
ALTER ROLE :"ro_user" SET lock_timeout = '5s';
ALTER ROLE :"ro_user" SET temp_file_limit = '256MB';
ALTER ROLE :"ro_user" SET work_mem = '32MB';

-- The read-only role must not be able to acquire privileges by any other route.
ALTER ROLE :"ro_user" NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER NOREPLICATION NOBYPASSRLS;
