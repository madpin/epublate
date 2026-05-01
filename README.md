# epublate

> A rich TUI that translates ePub story books with an LLM — preserving
> formatting and keeping a per-project **lore bible** so that
> characters, places, and events stay **consistent** across the entire
> book.

**Status:** v1 release-ready (`0.1.0`). All milestones M0 through M6
are landed: the project skeleton + CI, ePub round-trip, single-segment
translation, glossary v1 with cascade, Project Dashboard + batch + cost
meter / budget cap, helper-LLM extractor (book intake + pre-pass), and
M6 (high-contrast theme + global cheat sheet on `?` / `f1`, Settings
screen on `s`, snapshot baselines, opt-in `epubcheck` integration with
a `--strict` toggle, packaging polish, and a tag-triggered release
pipeline). See [`docs/PRD.md`](docs/PRD.md) for the full plan,
[`docs/USAGE.md`](docs/USAGE.md) for the curator walkthrough, and
[`CHANGELOG.md`](CHANGELOG.md) for what shipped when.

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
- **Tone presets** so the LLM gets the audience and register right out
  of the gate (literary fiction by default; presets for classic
  literature, historical fiction, children's picture books, middle
  grade, YA, fairytale / folklore, genre fiction, noir / hard-boiled
  crime, horror / gothic, cozy romance, explicit adult, humor /
  comedy, memoir / biography, poetry / verse, religious / spiritual,
  technical manuals, academic prose, and journalism). Curator picks
  one in the New Project modal or `epublate new --style-profile` and
  can swap / edit it later from Settings (PRD F-STYLE-1/2). The
  helper-LLM intake even *suggests* a preset based on its read of the
  source (F-STYLE-3) — and the New Project modal **auto-detects the
  right tone before you hit Create** by sniffing a few blocks of the
  picked ePub through the helper LLM (F-STYLE-4; toggle in Settings
  with `A`, env override `EPUBLATE_AUTO_TONE_SNIFF`).
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
```

### Try it on the sample book

The repo ships a real ePub at [`docs/Sample.epub`](docs/Sample.epub) so
you can take the M2 single-segment translation flow for a spin without
hunting for a book.

#### The TUI is the home — start here

```bash
# Launch the TUI. The Projects screen lists recently-opened projects
# (stored at ~/.config/epublate/recents.json) and lets you create or
# open projects without leaving the terminal.
uv run epublate --mock-llm

# In-TUI keys on the Projects screen:
#   n      → new project (modal: source ePub, target lang, out dir)
#   o      → open project by path
#   enter  → open the highlighted recent project
#   delete → drop the highlighted entry from recents (files untouched)
#   r      → refresh / prune missing entries
#   T      → cycle theme (dark / light / high-contrast)
#   ? / F1 → context-aware cheat sheet
#   q      → quit
```

The Dashboard, Reader, Glossary, and Inbox are all reachable from the
new-project / open-project flow above; pressing `q` on any inner
screen pops back to the Projects landing page.

#### Or skip the TUI for scripting / CI

```bash
# 1) Bootstrap a project from the sample (no LLM keys required).
#    `--out` must point at a fresh / empty directory; the command refuses
#    to overwrite an existing one. `rm -rf` first if you want to re-run.
rm -rf /tmp/epublate-sample
uv run epublate --mock-llm new docs/Sample.epub \
    --source-lang en --target-lang pt --out /tmp/epublate-sample

# 2) Open the Project Dashboard for a specific project.
#    Bindings: o = Reader, g = Glossary, i = Inbox, b = Batch,
#              B = Set/Clear budget cap, e = Intake (M5), s = Settings,
#              r = Refresh, q = Back. Global: T = cycle theme
#              (dark / light / high-contrast), ? or F1 = cheat sheet.
#    Reader: t = translate, a = accept, e = edit, r = retry,
#            j/k = next/prev segment, J/K = next/prev chapter, q = back.
uv run epublate --mock-llm open /tmp/epublate-sample

# 3) Headless: translate every pending segment via the worker pool.
#    Failures land in the Inbox; the run pauses if the budget cap is hit.
uv run epublate --mock-llm batch /tmp/epublate-sample \
    --concurrency 2 --budget 1.00

# 4) Triage: list flagged segments, proposed glossary entries, and alerts.
uv run epublate inbox /tmp/epublate-sample

# 5) Inspect spend / token / cache-hit stats for the project.
uv run epublate stats /tmp/epublate-sample --json

# 6) Manage the budget cap from the CLI (or via the Dashboard's `B` key).
uv run epublate budget set /tmp/epublate-sample 5.00
uv run epublate budget show /tmp/epublate-sample
uv run epublate budget clear /tmp/epublate-sample

# 7) Export the (possibly partial) translated ePub.
uv run epublate export /tmp/epublate-sample --out /tmp/epublate-sample.epub

# 7b) Validate the exported ePub against epubcheck (PRD F-IO-6, M6).
#     --epubcheck = warn-only summary; --strict = fail with exit 3 on
#     any error. Both require the optional [epubcheck] extra and Java.
uv run epublate export /tmp/epublate-sample \
    --out /tmp/epublate-sample.epub --strict
```

Swap `--mock-llm` for a real OpenAI-compatible endpoint by exporting
`EPUBLATE_LLM_BASE_URL`, `EPUBLATE_LLM_API_KEY`, and
`EPUBLATE_LLM_MODEL` (any combination of OpenAI, Azure, OpenRouter,
Together, Ollama, vLLM, or llama.cpp works — see PRD §6.1).
`EPUBLATE_LLM_HELPER_MODEL` (optional) selects a cheaper model for the
M5 helper-LLM extractor; if unset, the helper falls back to
`EPUBLATE_LLM_MODEL` (PRD F-LLM-2 — same endpoint, cheap model):

```bash
export EPUBLATE_LLM_BASE_URL=https://api.openai.com/v1
export EPUBLATE_LLM_API_KEY=sk-...
export EPUBLATE_LLM_MODEL=gpt-5-mini
export EPUBLATE_LLM_HELPER_MODEL=gpt-5-mini  # optional, defaults to $EPUBLATE_LLM_MODEL

uv run epublate open /tmp/epublate-sample
uv run epublate batch /tmp/epublate-sample --concurrency 2 --budget 5.00
```

For local dev you can drop the same variables into a `.env` file
instead of exporting them every shell session — see [`.env.example`](.env.example)
for a template:

```bash
cp .env.example .env   # then fill in EPUBLATE_LLM_API_KEY etc.
uv run epublate open /tmp/epublate-sample
```

The CLI auto-loads `./.env` first, then `<project_dir>/.env` for
project-scoped subcommands. Real shell variables always win, pytest
skips loading entirely so the test suite stays hermetic, and
`EPUBLATE_DISABLE_DOTENV=1` short-circuits loading for sealed CI jobs.
`.env` and `.envrc` are already in `.gitignore`, so secrets won't be
committed by accident.

```bash
# M5: helper-LLM book intake on a fresh project (opt-in, costs tokens).
uv run epublate new docs/Sample.epub \
    --source-lang en --target-lang pt \
    --out /tmp/epublate-sample --intake

# M5: re-run intake on an existing project at any time.
uv run epublate intake /tmp/epublate-sample --max-segments 30

# M5: opt-in pre-pass on batch — surfaces fresh proper nouns before
# each chapter so the translator's prompt sees them immediately.
uv run epublate batch /tmp/epublate-sample --extract
```

End users (after release) will install with:

```bash
uv tool install epublate                    # persistent install
uv tool install "epublate[epubcheck]"       # also wire post-export ePub validation
uvx epublate path/to/book.epub              # ephemeral run
```

The `[epubcheck]` extra installs the
[`epubcheck`](https://pypi.org/project/epubcheck/) Python wrapper
(which bundles the upstream Java JAR). It also requires a JRE on
your system. Without it, `epublate export --strict` still works —
it just prints a one-line "epubcheck skipped" summary instead of
running validation.

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
