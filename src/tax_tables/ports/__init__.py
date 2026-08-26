"""Ports: interfaces the domain and pipeline depend on, adapters implement."""

from tax_tables.ports.repository import DocumentHandle, IngestOutcome, RecordRepository

__all__ = ["DocumentHandle", "IngestOutcome", "RecordRepository"]
