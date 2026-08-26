-- ---------------------------------------------------------------------------
-- Saved reports.
--
-- A report is a *snapshot*, not a pointer. It could have been a row holding a run_id and nothing
-- else, rendered live on every read — and that would have been less code and less storage. It
-- would also mean a saved report silently changes when anything behind it changes: a metric
-- definition is revised, a chart is regenerated, the confidence arithmetic is tuned. Somebody
-- who saved a report in March and re-opens it in June would then be reading different figures
-- under the same name, with nothing to tell them so.
--
-- So the snapshot holds what the report *said when it was saved*: the question, the answer, the
-- charts, the SQL behind every cited number, the metric definition versions used, and the
-- confidence at that moment. The run_id stays alongside it, so the live trace is still one hop
-- away for anyone who wants to see what has changed since.
--
-- `name` is the only mutable field. Renaming a report is a labelling decision; editing what it
-- reports would defeat the point of saving it.
-- ---------------------------------------------------------------------------

CREATE TABLE agent.reports (
    report_id   UUID PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES agent.runs(run_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot    JSONB NOT NULL,

    -- A report with a blank name cannot be found again, which makes saving it pointless.
    CONSTRAINT reports_name_is_meaningful CHECK (length(btrim(name)) > 0),

    -- The two things that make a snapshot a report rather than a blob. Enforced here for the
    -- same reason findings_require_evidence is: a constraint cannot be forgotten by code
    -- written later.
    CONSTRAINT reports_snapshot_has_a_question CHECK (snapshot ? 'question'),
    CONSTRAINT reports_snapshot_has_an_answer CHECK (snapshot ? 'answer')
);

CREATE INDEX reports_recent_idx ON agent.reports (created_at DESC);
CREATE INDEX reports_run_idx ON agent.reports (run_id);

COMMENT ON TABLE agent.reports IS
    'A saved analysis, frozen as it read when it was saved. The run_id points at the live trace '
    'for anyone who wants to compare.';

COMMENT ON COLUMN agent.reports.snapshot IS
    'question, answer, confidence, findings, hypotheses, charts, evidence (with SQL) and the '
    'metric definition versions used. Immutable: only the name can be changed afterwards.';
