CREATE TABLE documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- SHA-256 of the raw bytes is the document's natural key: re-uploading
    -- the same PDF is a no-op (idempotency level 1 of 2). Level 2 moved on
    -- 2026-08-27 from the records natural key to a document-scoped replace;
    -- see the annotation in 0003_records.sql. This level is unaffected: the
    -- bytes hash deterministically, unlike anything a model produces.
    sha256       text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    filename     text NOT NULL,
    content_type text NOT NULL DEFAULT 'application/pdf',
    byte_size    bigint NOT NULL CHECK (byte_size > 0),
    page_count   integer CHECK (page_count > 0),
    -- decided by the extraction router: digital -> pdfplumber, scanned -> OCR
    source_kind  text CHECK (source_kind IN ('digital', 'scanned')),
    uploaded_at  timestamptz NOT NULL DEFAULT now()
);

-- Blob bytes live apart from document metadata so listing documents never
-- drags megabytes per row. This is the table behind the Postgres-bytea
-- BlobStore adapter (the Vercel default; S3 and filesystem adapters ignore it).
CREATE TABLE document_blobs (
    document_id uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    content     bytea NOT NULL
);

CREATE TABLE jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id       uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    status            text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt           integer NOT NULL DEFAULT 0,
    records_extracted integer,
    records_persisted integer,
    review_count      integer,
    error             jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz
);

-- At most one queued-or-running job per document: a duplicate upload finds
-- the live job instead of starting a second pipeline run.
CREATE UNIQUE INDEX jobs_one_live_per_document
    ON jobs (document_id) WHERE status IN ('queued', 'running');

-- The cron-sweep JobRunner polls by status; keep that scan off a seq scan.
CREATE INDEX jobs_status_idx ON jobs (status, created_at);
