-- ---------------------------------------------------------------------------
-- Multi-tenancy, and the four things that come with it.
--
-- Until now the schema had one tenant implicitly: every run, every report, every chart belonged
-- to whoever was running the service. Introducing organisations means every one of those rows now
-- has an owner, and the interesting question is not how to add the column - it is what happens to
-- the rows that already exist.
--
-- They are **backfilled into a default organisation**, not deleted and not left null. A nullable
-- organisation_id would mean every read has to decide what an unowned row means, and the first
-- place that decision is made carelessly is a tenant boundary leak. So the column is NOT NULL
-- from the moment it exists, and existing data belongs to the organisation that was implicitly
-- there all along.
--
-- Four more tables, each with a reason it is a table rather than a column:
--
-- * `api_keys` - the only way a caller proves which organisation it is. Only the *hash* is stored:
--   a key readable from the database is a key an operator can use as a customer.
-- * `data_sources` - `connection_config` holds credentials, so it is stored encrypted and the API
--   never returns it. See src/analyst_agent/security/crypto.py.
-- * `report_shares` - a share is a capability with a lifetime, so it is a row that can expire and
--   be revoked, not a boolean on the report.
-- * `alerts` / `alert_events` - a threshold is configuration; a breach is history. Keeping the
--   breach on the alert row would overwrite the last one every time it fired.
--
-- `audit_log` is append-only by intent: there is no UPDATE or DELETE path to it in the
-- repository, because an audit trail somebody can edit is not one.
-- ---------------------------------------------------------------------------

-- --- who ------------------------------------------------------------------

CREATE TABLE agent.users (
    user_id     UUID PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    display_name TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT users_email_looks_like_one CHECK (email LIKE '%@%')
);

CREATE TABLE agent.organizations (
    organization_id UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT organizations_name_is_meaningful CHECK (length(btrim(name)) > 0)
);

CREATE TABLE agent.organization_members (
    organization_id UUID NOT NULL REFERENCES agent.organizations(organization_id)
                    ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES agent.users(user_id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'analyst', 'viewer')),
    invited_by      UUID REFERENCES agent.users(user_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (organization_id, user_id)
);

CREATE INDEX organization_members_user_idx ON agent.organization_members (user_id);

COMMENT ON COLUMN agent.organization_members.role IS
    'owner: the last one cannot be removed or demoted. admin: manages the team and the data '
    'sources. analyst: asks questions, saves and shares reports. viewer: reads what is shared.';

-- --- how a caller proves which organisation it is -------------------------

CREATE TABLE agent.api_keys (
    key_id          UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES agent.organizations(organization_id)
                    ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES agent.users(user_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    -- SHA-256 of the key. The key itself is shown once, at creation, and never again.
    key_hash        TEXT NOT NULL UNIQUE,
    prefix          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);

CREATE INDEX api_keys_lookup_idx ON agent.api_keys (key_hash) WHERE revoked_at IS NULL;

COMMENT ON COLUMN agent.api_keys.key_hash IS
    'Only the hash. A key readable from the database is a key an operator can use as a customer.';

-- --- data sources ---------------------------------------------------------

CREATE TABLE agent.data_sources (
    data_source_id  UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES agent.organizations(organization_id)
                    ON DELETE CASCADE,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('postgres', 'csv', 'excel')),
    -- Fernet ciphertext. Never selected into any API response; see crypto.py for why the key
    -- lives in the environment rather than in this database.
    connection_config BYTEA NOT NULL,
    -- The parts safe to show: host, database, user, filename. No secret ever reaches this column.
    summary         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by      UUID REFERENCES agent.users(user_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    last_status     TEXT,

    CONSTRAINT data_sources_name_unique_per_org UNIQUE (organization_id, name),
    CONSTRAINT data_sources_name_is_meaningful CHECK (length(btrim(name)) > 0)
);

COMMENT ON COLUMN agent.data_sources.summary IS
    'The redacted view returned by the API: host, database, user, filename. A password reaching '
    'this column would defeat the encryption on the column beside it.';

-- --- sharing --------------------------------------------------------------

ALTER TABLE agent.reports
    ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('private', 'team', 'public'));

COMMENT ON COLUMN agent.reports.visibility IS
    'private: only the person who saved it. team: anybody in the organisation. public: anybody '
    'holding a link. Enforced in the repository query, not only in the route.';

CREATE TABLE agent.report_shares (
    share_id    UUID PRIMARY KEY,
    report_id   UUID NOT NULL REFERENCES agent.reports(report_id) ON DELETE CASCADE,
    -- Hashed for the same reason an API key is: a link readable from the database is a link an
    -- operator can follow.
    token_hash  TEXT NOT NULL UNIQUE,
    prefix      TEXT NOT NULL,
    audience    TEXT NOT NULL CHECK (audience IN ('team', 'public')),
    created_by  UUID REFERENCES agent.users(user_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    use_count   INTEGER NOT NULL DEFAULT 0,

    -- A share that expired before it was created is a bug, and one that cannot be used is worse
    -- than an error at creation time.
    CONSTRAINT report_shares_expiry_is_in_the_future CHECK (
        expires_at IS NULL OR expires_at > created_at
    )
);

CREATE INDEX report_shares_report_idx ON agent.report_shares (report_id);

-- --- alerts ---------------------------------------------------------------

CREATE TABLE agent.alerts (
    alert_id        UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES agent.organizations(organization_id)
                    ON DELETE CASCADE,
    name            TEXT NOT NULL,
    metric          TEXT NOT NULL,
    dimension       TEXT,
    -- 'drop' and 'spike' are relative to the recent baseline; 'below'/'above' are absolute.
    comparison      TEXT NOT NULL CHECK (comparison IN ('drop', 'spike', 'below', 'above')),
    threshold       NUMERIC NOT NULL,
    window_periods  INTEGER NOT NULL DEFAULT 6,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'triggered')),
    created_by      UUID REFERENCES agent.users(user_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    last_triggered_at TIMESTAMPTZ,

    CONSTRAINT alerts_name_unique_per_org UNIQUE (organization_id, name),
    CONSTRAINT alerts_threshold_is_positive CHECK (threshold > 0),
    CONSTRAINT alerts_window_is_usable CHECK (window_periods >= 2)
);

COMMENT ON CONSTRAINT alerts_window_is_usable ON agent.alerts IS
    'A baseline needs at least two prior periods. A one-period window would compare a value to '
    'itself and never fire, which reads as "nothing is wrong".';

CREATE TABLE agent.alert_events (
    event_id    UUID PRIMARY KEY,
    alert_id    UUID NOT NULL REFERENCES agent.alerts(alert_id) ON DELETE CASCADE,
    triggered   BOOLEAN NOT NULL,
    observed    NUMERIC,
    baseline    NUMERIC,
    change_pct  NUMERIC,
    period      TEXT,
    detail      TEXT NOT NULL,
    query_id    UUID REFERENCES agent.sql_audit(query_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX alert_events_recent_idx ON agent.alert_events (alert_id, created_at DESC);

COMMENT ON TABLE agent.alert_events IS
    'Every evaluation, fired or not. Recording only the breaches would leave no way to tell a '
    'quiet alert from a broken one.';

-- --- audit ----------------------------------------------------------------

CREATE TABLE agent.audit_log (
    entry_id        BIGSERIAL PRIMARY KEY,
    organization_id UUID REFERENCES agent.organizations(organization_id) ON DELETE SET NULL,
    actor_user_id   UUID REFERENCES agent.users(user_id) ON DELETE SET NULL,
    actor_label     TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_org_recent_idx ON agent.audit_log (organization_id, created_at DESC);

COMMENT ON TABLE agent.audit_log IS
    'Append-only by intent: the repository exposes no update or delete. An audit trail somebody '
    'can edit is not one. actor_label is kept alongside actor_user_id so an entry stays readable '
    'after the user row is gone.';

-- --- everything that already existed now has an owner ---------------------

INSERT INTO agent.organizations (organization_id, name, slug)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default organisation', 'default');

INSERT INTO agent.users (user_id, email, display_name)
VALUES ('00000000-0000-0000-0000-000000000002', 'analyst@example.com', 'Default analyst');

INSERT INTO agent.organization_members (organization_id, user_id, role)
VALUES ('00000000-0000-0000-0000-000000000001',
        '00000000-0000-0000-0000-000000000002', 'owner');

-- Added nullable, backfilled, then made NOT NULL. A column that stays nullable forces every read
-- to decide what an unowned row means, and the first careless decision there is a tenant leak.
ALTER TABLE agent.runs ADD COLUMN organization_id UUID
    REFERENCES agent.organizations(organization_id) ON DELETE CASCADE;
UPDATE agent.runs SET organization_id = '00000000-0000-0000-0000-000000000001';
ALTER TABLE agent.runs ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE agent.runs ALTER COLUMN organization_id
    SET DEFAULT '00000000-0000-0000-0000-000000000001';
CREATE INDEX runs_org_recent_idx ON agent.runs (organization_id, created_at DESC);

ALTER TABLE agent.reports ADD COLUMN organization_id UUID
    REFERENCES agent.organizations(organization_id) ON DELETE CASCADE;
UPDATE agent.reports SET organization_id = '00000000-0000-0000-0000-000000000001';
ALTER TABLE agent.reports ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE agent.reports ALTER COLUMN organization_id
    SET DEFAULT '00000000-0000-0000-0000-000000000001';
CREATE INDEX reports_org_recent_idx ON agent.reports (organization_id, created_at DESC);

ALTER TABLE agent.reports ADD COLUMN saved_by_user_id UUID
    REFERENCES agent.users(user_id) ON DELETE SET NULL;

COMMENT ON COLUMN agent.runs.organization_id IS
    'NOT NULL with a default, so a row cannot exist unowned even if a code path forgets to set '
    'it. The default is the organisation that was implicitly there before Phase 3.';
