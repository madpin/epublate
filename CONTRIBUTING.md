# Contributing to epublate

Thanks for taking the time to contribute. This file is the short version of
[`AGENTS.md`](AGENTS.md) for human contributors. The hard invariants live
there and in [`docs/PRD.md`](docs/PRD.md) — please read both before sending
a non-trivial PR.

## Bootstrap

`epublate` uses [`uv`](https://docs.astral.sh/uv/) for everything:
environments, dependency resolution, lockfile, test/lint runners, builds,
and end-user installation. **You only need `uv` on your `PATH`.** It will
install Python for you.

```bash
uv python install                 # installs the right Python (.python-version)
uv sync --all-extras --dev        # creates the venv + installs all deps
```

The bootstrap contract is:

```bash
uv sync --all-extras --dev && uv run pytest
```

must pass on a freshly cloned repo with **zero** other prerequisites and
**zero** network access. CI runs exactly the same commands.

## Day-to-day commands

```bash
uv run pytest                     # tests (no LLM keys, no network)
uv run pytest --snapshot-update   # regenerate Textual SVG baselines
uv run ruff check .               # lint
uv run ruff format --check .      # format check (use `format` to apply)
uv run mypy src/epublate          # types (strict on the core)
uv run epublate                   # launch the TUI
uv run epublate --mock-llm        # force the deterministic mock provider
uv build                          # wheel + sdist
```

Snapshot baselines for the TUI live under `tests/__snapshots__/` and
**are committed**. Re-run `uv run pytest --snapshot-update` after
intentional UI changes and review the regenerated `.raw` SVGs in your
PR diff. See [`docs/RELEASE.md`](docs/RELEASE.md) for the maintainer's
release runbook.

Don't introduce `pip`, `pipx`, `poetry`, `pyenv`, `tox`, `virtualenv`,
`setup.py`, `setup.cfg`, or `requirements.txt`. `pyproject.toml` (PEP 621)
is the single source of truth, and `uv.lock` is committed for reproducible
installs.

## Hard invariants (non-negotiable)

In priority order; see [`AGENTS.md`](AGENTS.md) §2 for the full list.

1. **Format preservation.** ePub structure and inline tags round-trip
   exactly via opaque placeholders. The model never sees raw HTML.
2. **Glossary consistency.** `locked` glossary entries are non-negotiable;
   the validator hard-fails violations.
3. **Resumability.** All state lives in one SQLite file per project, WAL
   mode, atomic writes. Crash-safe at all times.
4. **Local-first & private.** Book contents leave the machine only via
   the user-configured LLM/embedding endpoint.
5. **OpenAI-compatible LLMs only** in v1.
6. **`uv` is the only project tool.**

## Pre-PR checklist

Borrowed verbatim from [`AGENTS.md`](AGENTS.md) §11:

- [ ] `uv sync --all-extras --dev && uv run pytest` passes locally.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] `uv run mypy src/epublate` clean.
- [ ] Touched code is covered by a test (unit, property, or snapshot).
- [ ] PRD updated if a requirement, invariant, or open question changed.
- [ ] No new dependency without a one-line justification in the PR
      description.
- [ ] No `print`, no `TODO` without an issue link, no commented-out
      code.

New functional requirements get a stable PRD ID (`F-…`, `NFR-…`) and are
referenced from commit messages and the PR description.

## Reporting bugs

Please include:

- the command and the full output (stack trace if any),
- `uv --version` and `uv run python --version`,
- whether it reproduces with `uv run epublate --mock-llm`.

Never paste API keys or copyrighted book contents into an issue.
