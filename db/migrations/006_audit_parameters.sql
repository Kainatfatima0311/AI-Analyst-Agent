-- ---------------------------------------------------------------------------
-- Record the bound parameters alongside the statement.
--
-- The audit trail stored the SQL but not the values bound to it. For an ordinary statement that is
-- complete; for one rendered by `metric_query` it is not, because the statement contains
-- `%(date_from)s` placeholders and the values travelled separately. Reproducing such a query later
-- failed with a syntax error at the placeholder — found by the report view, which rebuilds a
-- result set by re-running the recorded statement.
--
-- That is a real gap in the project's central claim rather than a cosmetic one. "Every conclusion
-- is traceable to its queries" is hollow if a reviewer cannot re-run the query a figure came from,
-- and the parameterised path is exactly the one the metrics layer pushes work through.
--
-- Values only, never a secret: these are date windows and dimension filters chosen by the model
-- from declared names. Nothing on this path can carry a credential — a data source's configuration
-- lives encrypted in `data_sources` and never reaches a query's parameters.
-- ---------------------------------------------------------------------------

ALTER TABLE agent.sql_audit ADD COLUMN parameters JSONB;

COMMENT ON COLUMN agent.sql_audit.parameters IS
    'Values bound to the placeholders in sql_text, so the statement can be reproduced exactly. '
    'Null for a statement with no placeholders. Date windows and dimension filters only: a '
    'credential cannot reach this column, because a query never carries one.';
