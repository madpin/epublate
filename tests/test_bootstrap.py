"""Smoke tests confirming the package imports cleanly."""

from __future__ import annotations

import epublate


def test_version_is_a_non_empty_string() -> None:
    assert isinstance(epublate.__version__, str)
    assert epublate.__version__


def test_public_modules_import() -> None:
    from epublate import app, db, errors, llm  # noqa: F401  (smoke import)
