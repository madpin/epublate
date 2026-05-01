"""``.env`` loader for the ``epublate`` CLI.

The TUI / CLI reads its LLM endpoint from the process environment
(``EPUBLATE_LLM_BASE_URL`` / ``EPUBLATE_LLM_API_KEY`` / ``EPUBLATE_LLM_MODEL``
/ ``EPUBLATE_LLM_HELPER_MODEL``). On a developer machine that's tedious
to keep in sync with a shell rc, so we let users drop a ``.env`` next to
the project they're working on. The file is only ever loaded **into the
current process** — we never write it back, and we never override real
shell variables (``override=False``).

Resolution order (first hit wins, courtesy of ``override=False``):

1. ``./.env`` — the cwd from which ``epublate`` was launched.
2. ``<project_dir>/.env`` — only for project-scoped subcommands.

Tests stay hermetic: when pytest is the runner the loader becomes a
no-op unless the caller passes ``force=True``. That keeps the suite
deterministic regardless of what's lying around in a developer's
working tree (PRD NFR-7, ``tests/conftest.py`` docstring).

``.env`` and ``.envrc`` are already in the repository's ``.gitignore``,
so committing secrets by accident isn't possible through the normal
add-all flow.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_logger = logging.getLogger(__name__)

DOTENV_FILENAME = ".env"

# Opt-out knob — set ``EPUBLATE_DISABLE_DOTENV=1`` to skip loading even
# outside pytest (useful for CI jobs that want a sealed environment).
ENV_DISABLE = "EPUBLATE_DISABLE_DOTENV"


def _running_under_pytest() -> bool:
    """Best-effort detection of an active pytest session.

    Pytest sets ``PYTEST_VERSION`` (since 8.x) and imports the ``pytest``
    module before any test code runs; either signal is sufficient. We
    check both so behavior is identical whether the loader is invoked
    from a test importing the CLI or from a subprocess started by a
    test (``CliRunner.invoke``).
    """

    if os.environ.get("PYTEST_VERSION"):
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def load_dotenv_files(
    *,
    project_dir: Path | None = None,
    cwd: Path | None = None,
    force: bool = False,
) -> list[Path]:
    """Load ``.env`` files into the process environment.

    Returns the list of files that were actually applied, in load order.
    Real shell variables always win — ``override=False`` is hard-coded.

    Parameters
    ----------
    project_dir:
        If given, also try ``<project_dir>/.env`` after the cwd file.
    cwd:
        Override the cwd used to find ``./.env``. Defaults to
        :func:`pathlib.Path.cwd`. Exposed for tests.
    force:
        Bypass the pytest auto-skip. Loader unit tests pass this; nothing
        else should.
    """

    if not force:
        if os.environ.get(ENV_DISABLE) == "1":
            return []
        if _running_under_pytest():
            return []

    base_cwd = cwd if cwd is not None else Path.cwd()
    candidates: list[Path] = [base_cwd / DOTENV_FILENAME]
    if project_dir is not None:
        project_path = project_dir / DOTENV_FILENAME
        # Avoid double-loading when a user runs ``epublate`` from inside
        # the project folder — same path resolves to the same file.
        try:
            same = project_path.resolve() == candidates[0].resolve()
        except OSError:
            same = False
        if not same:
            candidates.append(project_path)

    loaded: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        # ``override=False`` so real env vars (and earlier-loaded files)
        # take precedence — first hit wins, real shell wins over both.
        if load_dotenv(path, override=False):
            loaded.append(path)
            _logger.debug("loaded dotenv file: %s", path)
    return loaded


__all__ = [
    "DOTENV_FILENAME",
    "ENV_DISABLE",
    "load_dotenv_files",
]
