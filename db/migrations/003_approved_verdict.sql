-- ---------------------------------------------------------------------------
-- Add an `approved` verdict to sql_audit.
--
-- Until now a query had three possible verdicts: allowed, rejected, escalated. That left no way
-- to record the outcome that actually matters most for review - a query the guard escalated and
-- a *human* then cleared. Recording it as `allowed` would erase the escalation from the trail;
-- leaving it as `escalated` collided with sql_audit_executed_implies_allowed, which is the
-- constraint that stops a rejected query being marked executed.
--
-- So `approved` is its own verdict. The audit now distinguishes "the guard permitted this" from
-- "a person permitted this", which is exactly the distinction a reviewer is looking for, and the
-- executed constraint accepts both while still refusing rejected and undecided-escalated ones.
-- ---------------------------------------------------------------------------

ALTER TABLE agent.sql_audit DROP CONSTRAINT sql_audit_verdict_check;

ALTER TABLE agent.sql_audit ADD CONSTRAINT sql_audit_verdict_check CHECK (
    verdict IN ('allowed', 'rejected', 'escalated', 'approved')
);

ALTER TABLE agent.sql_audit DROP CONSTRAINT sql_audit_executed_implies_allowed;

ALTER TABLE agent.sql_audit ADD CONSTRAINT sql_audit_executed_implies_allowed CHECK (
    NOT executed OR verdict IN ('allowed', 'approved')
);

COMMENT ON CONSTRAINT sql_audit_executed_implies_allowed ON agent.sql_audit IS
    'Only a query the guard allowed, or one a human approved, may be recorded as executed. '
    'A rejected query, or one still awaiting a decision, may not.';

COMMENT ON COLUMN agent.sql_audit.verdict IS
    'allowed: cleared by the guard. rejected: never ran. escalated: needs a human and has not '
    'run. approved: escalated by the guard and then cleared by a named human, whose decision is '
    'in agent.approvals.';
