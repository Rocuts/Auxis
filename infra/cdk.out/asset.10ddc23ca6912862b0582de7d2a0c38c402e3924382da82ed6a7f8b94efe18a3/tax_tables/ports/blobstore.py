"""BlobStore port — where the raw PDF bytes live between upload and the
pipeline run.

Three adapters by target (CLAUDE.md): S3 on AWS, Postgres ``bytea`` on
Vercel (the default; see the blob ADR), local filesystem for docker-compose.
``PostgresRecordRepository`` implements this port alongside
``RecordRepository`` — the ``document_blobs`` table lives in the same
database and the blob is written in the same connection's transaction as the
document row, so an upload can never register a document whose bytes were
lost.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class BlobStore(Protocol):
    def store_blob(self, document_id: UUID, content: bytes) -> None:
        """Persist the raw bytes for a registered document (idempotent:
        re-storing the same document's bytes is a no-op overwrite)."""
        ...

    def load_blob(self, document_id: UUID) -> bytes:
        """The stored bytes. Raises ``KeyError`` if the document has no
        blob — a job pointing at a blobless document is a real fault, not
        an empty PDF."""
        ...
