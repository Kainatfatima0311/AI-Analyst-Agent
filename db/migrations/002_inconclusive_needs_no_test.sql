-- ---------------------------------------------------------------------------
-- Refine hypotheses_require_a_test.
--
-- The original constraint said: a hypothesis may not leave `proposed` without at least one test
-- query. The intent was right and the scope was slightly too wide, which the multi-hypothesis
-- tests surfaced the first time they ran.
--
-- `supported` and `refuted` are claims *about the data*. Neither may be recorded without a query
-- behind it - that is the whole traceability argument, and it is unchanged.
--
-- `inconclusive` asserts the opposite: that nothing could be concluded. It is exactly the state a
-- hypothesis lands in when its test could not run at all, or when the test it would have run is
-- the same statement a sibling already used and so could never separate the two. Demanding a
-- query for that verdict forces one of two bad outcomes: attach some other hypothesis's query and
-- misrepresent what was tested, or leave the hypothesis stuck at `proposed` - which permanently
-- blocks the synthesis gate, since that gate counts only terminal statuses.
--
-- So: a query is required for a verdict that claims something, and not for one that declines to.
-- ---------------------------------------------------------------------------

ALTER TABLE agent.hypotheses DROP CONSTRAINT hypotheses_require_a_test;

ALTER TABLE agent.hypotheses ADD CONSTRAINT hypotheses_require_a_test CHECK (
    status IN ('proposed', 'inconclusive') OR cardinality(test_query_ids) > 0
);

COMMENT ON CONSTRAINT hypotheses_require_a_test ON agent.hypotheses IS
    'supported and refuted are claims about the data and need a query behind them. '
    'inconclusive is a claim that nothing could be concluded, and is the correct state for a '
    'hypothesis whose test could not run or could not discriminate it from a sibling.';

-- The reasoning is now what carries the explanation for an inconclusive verdict, so it may not
-- be left blank: "inconclusive" with no stated reason is indistinguishable from a bug.
ALTER TABLE agent.hypotheses ADD CONSTRAINT hypotheses_inconclusive_is_explained CHECK (
    status <> 'inconclusive' OR (reasoning IS NOT NULL AND length(trim(reasoning)) > 0)
);
