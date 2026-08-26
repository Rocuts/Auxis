-- Widen the audit-trail rule to EVERY exit from 'open' (adversarial-review
-- minor, promoted): 0007 guarded only 'resolved', so a 'dismissed' row could
-- close unaudited. Now any closed row needs who and when; 'resolved'
-- additionally keeps its resolution payload ('dismissed' may carry one, but
-- a dismissal is a judgment that no payload is needed, so it is optional).
-- Constraints are replaced, never edited in place (0005 precedent).
ALTER TABLE review_queue DROP CONSTRAINT resolved_rows_carry_audit_trail;
ALTER TABLE review_queue
    ADD CONSTRAINT closed_rows_carry_audit_trail
    CHECK (
        status = 'open'
        OR (resolved_by IS NOT NULL
            AND resolved_at IS NOT NULL
            AND (status <> 'resolved' OR resolution IS NOT NULL))
    );
