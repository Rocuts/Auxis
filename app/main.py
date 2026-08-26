"""Vercel entrypoint.

Vercel detects a FastAPI app by looking for an instance named ``app`` at a
supported entrypoint — ``app.py`` / ``index.py`` / ``server.py`` / ``main.py``
/ ``wsgi.py`` / ``asgi.py``, at the root or inside ``src/`` or ``app/``. The
real application lives at ``src/tax_tables/api/main.py``, which is nested too
deep to be discovered, so this file is the shim that surfaces it. The whole
app becomes one Vercel Function; no rewrites are needed and none are
configured.

Nothing target-specific is decided here. The adapters are chosen from the
environment (`JOB_RUNNER=vercel`, `EXTRACTION_OCR_ENGINE=vision`), which is
what makes this a shim rather than a second composition root.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # installed normally by `uv sync`, which installs the project itself
    from tax_tables.api.main import app
except ModuleNotFoundError:  # pragma: no cover - platform installer fallback
    # Belt and braces: if the builder installs dependencies without the root
    # package, a src/ layout is unimportable and the deploy fails at import
    # time with a message that looks like a missing dependency. Cheaper to
    # make it impossible than to diagnose it from a build log.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from tax_tables.api.main import app

__all__ = ["app"]
