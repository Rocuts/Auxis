"""API settings, read once from the environment at app construction.

The same fail-closed posture as the adapter configs: a missing secret is a
startup error, never an open endpoint. ``repr`` hides both secrets
(anti-goal #10).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Uploads above this are rejected before any byte reaches the pipeline.
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
#: A sane page cap for tax-table documents; the fixtures peak at 2 pages.
DEFAULT_MAX_PAGES = 25
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ApiConfigError(RuntimeError):
    """The environment does not describe a servable API."""


@dataclass(frozen=True)
class ApiSettings:
    database_url: str = field(repr=False)
    api_key: str = field(repr=False)
    cron_secret: str = field(repr=False)
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_pages: int = DEFAULT_MAX_PAGES
    default_page_size: int = DEFAULT_PAGE_SIZE
    max_page_size: int = MAX_PAGE_SIZE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiSettings:
        source = os.environ if env is None else env
        missing = [
            name for name in ("DATABASE_URL", "API_KEY", "CRON_SECRET") if not source.get(name)
        ]
        if missing:
            raise ApiConfigError(f"missing required environment: {', '.join(missing)}")
        return cls(
            database_url=source["DATABASE_URL"],
            api_key=source["API_KEY"],
            cron_secret=source["CRON_SECRET"],
            max_upload_bytes=int(source.get("MAX_UPLOAD_BYTES") or DEFAULT_MAX_UPLOAD_BYTES),
            max_pages=int(source.get("MAX_PAGES") or DEFAULT_MAX_PAGES),
        )
