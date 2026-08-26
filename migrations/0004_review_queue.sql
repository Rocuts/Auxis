-- A cell that cannot be parsed confidently lands here with its provenance
-- attached — never silently dropped, never guessed (anti-goal #8).
CREATE TABLE review_queue (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    source_page integer,
    table_id    text,
    row_index   integer,
    col_index   integer,
    raw_value   text,
    reason      text NOT NULL,
    status      text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX review_queue_open_idx
    ON review_queue (document_id) WHERE status = 'open';

-- Gap-freeness is a cross-row aggregate, so it cannot be an exclusion
-- constraint. This diagnostic view names every hole between adjacent
-- brackets in a chain; in canonical half-open form, adjacency means
-- lower(next) = upper(prev), so anything else is a gap.
CREATE VIEW bracket_gaps AS
WITH ordered AS (
    SELECT jurisdiction,
           record_type,
           tax_year,
           filing_status,
           taxpayer_class,
           lifecycle_status,
           bracket,
           lag(upper(bracket)) OVER (
               PARTITION BY jurisdiction, record_type, tax_year,
                            filing_status, taxpayer_class, lifecycle_status
               ORDER BY lower(bracket)
           ) AS prev_upper
    FROM records
    WHERE bracket IS NOT NULL
)
SELECT jurisdiction,
       record_type,
       tax_year,
       filing_status,
       taxpayer_class,
       lifecycle_status,
       prev_upper AS gap_starts_at,
       lower(bracket) AS gap_ends_at
FROM ordered
WHERE prev_upper IS NOT NULL
  AND lower(bracket) IS DISTINCT FROM prev_upper;
