"""Adapters: concrete implementations of the ports, selected by configuration."""

from tax_tables.adapters.postgres import PostgresRecordRepository

__all__ = ["PostgresRecordRepository"]
