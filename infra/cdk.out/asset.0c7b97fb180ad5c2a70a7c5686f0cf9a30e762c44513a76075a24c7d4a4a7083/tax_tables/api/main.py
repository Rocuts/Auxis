"""ASGI entrypoint: ``uvicorn tax_tables.api.main:app`` (see ``make api``).

Settings come from the environment at import; a missing secret fails the
boot, never opens an endpoint.
"""

from tax_tables.api.app import create_app
from tax_tables.api.settings import ApiSettings

app = create_app(ApiSettings.from_env())
