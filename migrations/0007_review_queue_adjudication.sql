-- Adjudication audit trail (ADR 012). `resolution` holds the adjudicator's
-- citated payload as jsonb: on a row still 'open' it is a stored proposal
-- awaiting a human; on a 'resolved' row it is the audit record of what
-- resolved the item. `resolved_by` names the resolver ('adjudicator:<model>'
-- or a human identity); `resolved_at` completes the trail.
ALTER TABLE review_queue
    ADD COLUMN resolution  jsonb,
    ADD COLUMN resolved_by text,
    ADD COLUMN resolved_at timestamptz;

-- A resolved item without its audit trail is unrepresentable: auto-resolution
-- is acceptable only because it is always accountable.
ALTER TABLE review_queue
    ADD CONSTRAINT resolved_rows_carry_audit_trail
    CHECK (
        status <> 'resolved'
        OR (resolution IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
    );
