"""Vercel entrypoint.

Vercel's FastAPI preset looks for a top-level instance named ``app`` at a
supported entrypoint — ``app.py`` / ``index.py`` / ``server.py`` / ``main.py``
/ ``wsgi.py`` / ``asgi.py``, at the root or inside ``src/`` or ``app/``. The
real application lives at ``src/tax_tables/api/main.py``, which is nested too
deep to be discovered, so this file surfaces it.

The import is unconditional and top-level on purpose: the detector reads this
statically, and wrapping it in a ``try`` hides ``app`` from it (the build
fails with "does not define a top-level app FastAPI instance", verified).

Nothing target-specific is decided here. The adapters come from the
environment (``JOB_RUNNER=vercel``, ``EXTRACTION_OCR_ENGINE=vision``), which
is what keeps this a shim rather than a second composition root.
"""

from tax_tables.api.main import app

__all__ = ["app"]
