"""Tax Table Ingestion Service.

Accepts PDF documents containing tax tables, extracts the tabular data,
normalizes it into a canonical schema, persists it, and exposes it over REST.
"""

__version__ = "0.1.0"
