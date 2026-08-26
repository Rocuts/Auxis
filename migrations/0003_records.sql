-- The canonical fact table: one typed core for every record shape, plus a
-- JSONB tail for type-specific attributes. Not eleven tables (every new
-- document shape would need a migration), not one blob (no constraints).
CREATE TABLE records (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Provenance: every value traces to a table on a page of a document.
    document_id      uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    source_page      integer NOT NULL CHECK (source_page > 0),
    table_id         text NOT NULL,

    -- Temporal validity. tax_year is nullable because document 03's sales-tax
    -- rates carry only an effective date; bracket records always have it
    -- (enforced below).
    tax_year         integer CHECK (tax_year BETWEEN 1900 AND 2999),
    effective_from   date,
    effective_to     date,
    lifecycle_status text NOT NULL DEFAULT 'active'
                     CHECK (lifecycle_status IN ('active', 'superseded')),

    -- Discriminators. record_type is the shape; attribute_key is the
    -- sub-discriminator (employment component, wage-base item, surtax name,
    -- payroll period, deduction condition, gain category).
    jurisdiction     text NOT NULL,
    record_type      text NOT NULL CHECK (record_type IN (
                         'ordinary_income_bracket',
                         'preferential_gain_bracket',
                         'special_gain_rate',
                         'standard_deduction',
                         'additional_standard_deduction',
                         'dependent_deduction_rule',
                         'sales_tax_rate',
                         'employment_tax_rate',
                         'wage_base',
                         'surtax_threshold',
                         'withholding_allowance')),
    attribute_key    text,
    filing_status    text CHECK (filing_status IN (
                         'single',
                         'married_filing_jointly',
                         'married_filing_separately',
                         'head_of_household')),
    taxpayer_class   text,

    -- Typed value slots. Bracket bounds arrive as inclusive whole-currency
    -- integers ([12251, 49800]); int8range canonicalizes them to half-open
    -- ([12251, 49801)), which makes adjacency exact: lower(next) = upper(prev).
    -- A NULL upper bound ("and over") is an unbounded range. The rate domain
    -- deliberately admits small negatives: document 03 contains a legitimate
    -- negative local rate (statutory rebate). A rate >= 0 check rejects valid data.
    bracket          int8range,
    rate             numeric(9, 6) CHECK (rate > -1 AND rate < 1),
    amount           numeric(14, 2),
    currency         char(3),

    -- Type-specific tail (employer/employee/self-employed rate triples,
    -- *_pct columns, prior-year values, prose rules, notes).
    attrs            jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- Pipeline quality. A low-confidence value is persisted for review, never
    -- silently dropped or guessed (anti-goal #8).
    confidence       numeric(4, 3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    review_status    text NOT NULL DEFAULT 'clean'
                     CHECK (review_status IN ('clean', 'needs_review')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- Audit trail for the upsert semantics: a same-document re-ingest
    -- refreshes updated_at, so "when was this value last confirmed" is
    -- answerable.
    updated_at       timestamptz NOT NULL DEFAULT now(),

    -- A bracket without its discriminator chain would silently escape the
    -- exclusion constraint below; forbid that shape outright.
    CONSTRAINT bracket_requires_chain CHECK (
        bracket IS NULL OR (tax_year IS NOT NULL AND filing_status IS NOT NULL)
    ),

    CONSTRAINT effective_window CHECK (
        effective_from IS NULL OR effective_to IS NULL
        OR effective_to >= effective_from
    ),

    -- Idempotency level 2 of 2: re-ingesting a document upserts on this
    -- natural key instead of duplicating records. NULLS NOT DISTINCT makes
    -- two NULLs equal, so scalar records (bracket IS NULL) collide correctly.
    CONSTRAINT records_natural_key UNIQUE NULLS NOT DISTINCT (
        jurisdiction, record_type, attribute_key, tax_year,
        filing_status, taxpayer_class, lifecycle_status, bracket
    ),

    -- The centerpiece: overlapping brackets are unrepresentable at the
    -- database level for the same (jurisdiction, record_type, tax_year,
    -- filing_status, taxpayer_class, lifecycle_status) chain.
    --   * btree_gist provides the scalar-equality operators inside GiST.
    --   * COALESCE exists because NULL never conflicts with anything in an
    --     exclusion constraint. taxpayer_class strictly needs it (document
    --     05's brackets carry none); filing_status cannot be NULL here today
    --     thanks to bracket_requires_chain, and its COALESCE is deliberate
    --     defense in depth so the constraint stays self-sufficient if that
    --     CHECK ever loosens.
    --   * lifecycle_status is part of the chain: an active set and the
    --     superseded set it replaced legitimately cover the same ranges,
    --     while each set stays internally overlap-free.
    --   * && on int8range flags any overlap, including with unbounded tops.
    --   * PG18's UNIQUE ... WITHOUT OVERLAPS was evaluated and rejected: it
    --     compiles to this same GiST exclusion but cannot take a WHERE
    --     predicate, so in a polymorphic table it would bind scalar records
    --     (bracket IS NULL) too, and it would force sentinel values into
    --     nullable discriminators that the canonical schema keeps as NULL.
    CONSTRAINT no_overlapping_brackets EXCLUDE USING gist (
        jurisdiction                    WITH =,
        record_type                     WITH =,
        tax_year                        WITH =,
        (COALESCE(filing_status, ''))   WITH =,
        (COALESCE(taxpayer_class, ''))  WITH =,
        lifecycle_status                WITH =,
        bracket                         WITH &&
    ) WHERE (bracket IS NOT NULL)
);

-- The API's main read path: GET /records?tax_year=&jurisdiction=&record_type=
-- filtered to active records.
CREATE INDEX records_query_idx
    ON records (tax_year, jurisdiction, record_type, lifecycle_status);

-- Cursor pagination is keyset on (created_at, id).
CREATE INDEX records_cursor_idx ON records (created_at, id);

-- Note for GET /records/resolve: the GiST index behind
-- no_overlapping_brackets IS the range index that serves bracket lookups —
-- but only when queries use the same indexed expressions, i.e.
-- COALESCE(filing_status, '') / COALESCE(taxpayer_class, ''), not bare
-- column equality. The repository adapter must query in that form.
