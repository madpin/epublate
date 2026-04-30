# epublate

> A rich TUI that translates ePub story books with an LLM — preserving
> formatting and keeping a per-project **lore bible** so that
> characters, places, and events stay **consistent** across the entire
> book.

**Status:** early development (pre-M0). The product spec is complete;
implementation is just getting started. See
[`docs/PRD.md`](docs/PRD.md) for the full plan and roadmap.

---

## Why

Translating long story books with off-the-shelf LLM tools fails in
three ways: format gets flattened, proper-noun translations drift
("Élise" → "Elise" → "Eliza" across chapters), and each call has no
memory of prior decisions. `epublate` fixes all three:

- **Format preservation.** ePub structure, inline tags, footnotes, and
  assets round-trip exactly. The model never sees raw HTML.
- **Consistency.** A per-project glossary tracks characters, places,
  events, items, and recurring phrases. Locked entries are enforced by
  a mechanical validator — drift is impossible by construction.
- **Memory.** Every segment, decision, and LLM call is persisted in a
  single SQLite file. Quit and resume mid-book with zero rework.

## Features (planned for v1)

- ePub 2 and ePub 3 round-trip with structural preservation.
- Per-project lore bible with three-tier status (`proposed` /
  `confirmed` / `locked`) and cascade re-translation on changes.
- Works with **any OpenAI-compatible** chat-completions endpoint
  (OpenAI, Azure, OpenRouter, Together, Ollama, vLLM, llama.cpp).
- Rich Textual TUI: side-by-side reader, glossary editor, batch mode
  with curator inbox, cost meter and budget caps.
- Resumable: SQLite WAL mode, atomic exports, append-only event log.
- Optional embeddings for style retrieval and fuzzy entity matching.

PDF support is a **non-goal in v1**, but the format-handling layer is
designed to make it pluggable later.

## Quickstart (development)

This project uses [`uv`](https://docs.astral.sh/uv/) for everything —
environment, dependencies, tests, builds. You only need `uv` on your
`PATH`; it will install Python for you.

```bash
git clone https://github.com/<you>/epublate.git
cd epublate

uv python install                 # installs the right Python (.python-version)
uv sync --all-extras --dev        # creates the venv + installs deps

uv run pytest                     # tests (no network, no LLM keys needed)
uv run ruff check .               # lint
uv run mypy src/epublate          # types

uv run epublate --mock-llm new tests/fixtures/sample.epub
```

End users (after release) will install with:

```bash
uv tool install epublate          # persistent install
uvx epublate path/to/book.epub    # ephemeral run
```

## Project layout (planned)

```
epublate/
  pyproject.toml         # PEP 621 metadata + deps (single source of truth)
  uv.lock                # committed for reproducible installs
  .python-version        # pinned Python; uv installs it on demand
  src/epublate/
    app/                 # Textual UI
    core/                # project, pipeline, segmentation, validators, cache
    formats/             # base.py + epub.py (pdf.py later)
    llm/                 # OpenAI-compatible client + prompts
    glossary/            # the lore bible
    embeddings/          # optional
    db/                  # schema + migrations
  tests/                 # pytest suite + fixtures
  docs/PRD.md            # the spec
  AGENTS.md              # cross-tool agent guidance
  CLAUDE.md              # → AGENTS.md (symlink)
  .cursor/rules/         # Cursor rules
```

## Documentation

- [`docs/PRD.md`](docs/PRD.md) — product requirements, architecture,
  schema, prompting strategy, roadmap, open questions.
- [`AGENTS.md`](AGENTS.md) — invariants, conventions, and rules for
  AI agents (Cursor, Claude Code, Codex, Aider, …) working on the
  codebase.
- [`.cursor/rules/`](.cursor/rules/) — focused, scoped Cursor rules.

## Contributing

1. Read [`AGENTS.md`](AGENTS.md). The hard invariants there are
   non-negotiable (format preservation, glossary consistency,
   resumability, local-first, OpenAI-compatible only, `uv`-only).
2. Bootstrap with `uv sync --all-extras --dev`.
3. Before opening a PR:
   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/epublate
   ```
4. New requirements get a stable PRD ID (`F-…`, `NFR-…`) and are
   referenced from the PR description.

## License

[MIT](LICENSE).
