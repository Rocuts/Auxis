"""Phase 0 smoke test: the package imports and the toolchain runs end to end."""

import tax_tables


def test_package_imports() -> None:
    assert tax_tables.__version__ == "0.1.0"
