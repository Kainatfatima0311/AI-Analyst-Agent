-- ---------------------------------------------------------------------------
-- Grants. Control C1 and C3 in docs/security-controls.md.
--
-- analyst_ro gets exactly this and nothing more:
--   * CONNECT on the database
--   * USAGE on schema analytics
--   * SELECT on the tables and views that exist in analytics, now and in future
--
-- It is granted nothing on schema agent, nothing on public, and no write privilege
-- anywhere. The default privileges clause means a table added later is readable without
-- also becoming writable.
-- ---------------------------------------------------------------------------

GRANT CONNECT ON DATABASE :"db_name" TO :"ro_user";

GRANT USAGE ON SCHEMA analytics TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO :"ro_user";
GRANT SELECT ON ALL SEQUENCES IN SCHEMA analytics TO :"ro_user";

ALTER DEFAULT PRIVILEGES FOR ROLE :"app_user" IN SCHEMA analytics
    GRANT SELECT ON TABLES TO :"ro_user";

-- Explicitly withhold everything else, so a later grant to PUBLIC cannot leak in.
REVOKE ALL ON SCHEMA agent   FROM :"ro_user";
REVOKE ALL ON SCHEMA public  FROM :"ro_user";
REVOKE ALL ON ALL TABLES IN SCHEMA agent FROM :"ro_user";

-- Functions are not granted wholesale: nothing in analytics defines any, and the
-- validator's function denylist (control C2) is the primary control regardless.
