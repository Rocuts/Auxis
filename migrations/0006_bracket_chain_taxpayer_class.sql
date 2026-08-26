-- Two adversarial-review findings against the Phase 2a diff, both about
-- brackets that live at the edges of a chain. 0003/0004 are applied
-- everywhere, so both objects are replaced here, never edited in place.
--
-- (1) Document 01's Estates and Trusts schedule holds four legitimate
-- ordinary_income_bracket records discriminated by taxpayer_class
-- ('estate_or_trust') with NO filing_status. bracket_requires_chain
-- demanded filing_status specifically, making them unpersistable. The rule
-- it meant to state is "a bracket needs a taxpayer discriminator". The
-- exclusion constraint needs no change: its COALESCE(filing_status, '')
-- was documented in 0003 as defense in depth for exactly this loosening,
-- and is now load-bearing — NULL-filing_status brackets still chain and
-- still exclude overlaps.
ALTER TABLE records
    DROP CONSTRAINT bracket_requires_chain;

ALTER TABLE records
    ADD CONSTRAINT bracket_requires_chain CHECK (
        bracket IS NULL
        OR (tax_year IS NOT NULL
            AND (filing_status IS NOT NULL OR taxpayer_class IS NOT NULL))
    );

-- (2) The bracket_gaps view walked pairs, so a chain missing its LOWEST
-- bracket produced no pair to compare and no row. Bounds are canonically
-- >= 0, so anchoring lag's default at 0 makes an uncovered head visible:
-- a multi-bracket chain whose first bracket starts above 0 now reports the
-- hole [0, lower). Single-bracket chains are exempt — a lone threshold
-- record legitimately starts high (mirrors validators.check_bracket_bottom).
CREATE OR REPLACE VIEW bracket_gaps AS
WITH ordered AS (
    SELECT jurisdiction,
           record_type,
           tax_year,
           filing_status,
           taxpayer_class,
           lifecycle_status,
           bracket,
           lag(upper(bracket), 1, 0::bigint) OVER (
               PARTITION BY jurisdiction, record_type, tax_year,
                            filing_status, taxpayer_class, lifecycle_status
               ORDER BY lower(bracket)
           ) AS prev_upper,
           count(*) OVER (
               PARTITION BY jurisdiction, record_type, tax_year,
                            filing_status, taxpayer_class, lifecycle_status
           ) AS chain_size
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
WHERE chain_size > 1
  AND lower(bracket) IS DISTINCT FROM prev_upper;
