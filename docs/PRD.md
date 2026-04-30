# epublate — Product Requirements Document

> A rich TUI application that translates ePub books with an LLM while
> preserving formatting and keeping a long-lived "lore bible" so that
> names, places, events, and other narrative entities are translated
> **consistently** across the entire book.

---

## 1. Overview

### 1.1 Problem

Translating long story books (novels, sagas, light novels) with off-the-shelf
LLM tools fails in three predictable ways:

1. **Format loss.** Naive pipelines flatten HTML/XHTML, breaking ePub
   structure (chapters, headings, italics, footnotes, ruby text, images).
2. **Inconsistency.** A character named *"Élise"* may be rendered as
   *"Elise"* in chapter 2, *"Eliza"* in chapter 7, and *"Elisa"* in
   chapter 14. Place names, magic systems, organizations, and recurring
   phrases drift the same way.
3. **No memory.** Each LLM call lacks the context the human translator
   would carry in their head: who this character is, how they speak,
   what was already established.

### 1.2 Vision

`epublate` is an **interactive, terminal-native** translation studio. It
treats translation as a multi-pass pipeline backed by a project database
("the lore bible") that the LLM and the human curator both read and
write to. The user drives translation **chapter by chapter, segment by
segment**, with the option to auto-run batches when they trust the
glossary.

### 1.3 Naming

`epublate` = **epub** + transl**ate**. Pronounced "ep-yoo-blate".

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1)

- **G1.** Translate ePub 2 and ePub 3 files end-to-end while preserving
  the original structure (spine, manifest, TOC, CSS, images, inline
  formatting, footnotes).
- **G2.** Maintain a per-project **lore bible** (SQLite) of entities
  (characters, places, events, dates, items, organizations, recurring
  phrases) with **locked canonical translations**.
- **G3.** Use any **OpenAI-compatible** chat-completion endpoint
  (OpenAI, Azure OpenAI, OpenRouter, Together, local llama.cpp /
  vLLM / Ollama servers) via a single configurable base URL + key.
- **G4.** Provide a **rich TUI** (Textual) with: project browser,
  chapter/segment view, side-by-side diff, glossary editor, cost meter,
  log/event stream.
- **G5.** Be **resumable**: every LLM call, decision, and edit is
  persisted. The user can quit and resume mid-book without losing state.
- **G6.** Be **consistent**: every translated segment is post-validated
  against the glossary; violations are flagged and can be auto-fixed or
  surfaced for review.
- **G7.** Produce a valid, openable ePub at any point (partial
  translation is allowed; untranslated segments fall back to source).

### 2.2 Stretch Goals (v1.x)

- **S1.** Embedding-based retrieval to fetch relevant prior context
  (style, prior mentions of an entity, similar passages) for each LLM
  call. Pluggable; off by default.
- **S2.** Multiple LLM "roles" in the pipeline (extractor, translator,
  reviewer/critic, fixer) — a small agentic loop.
- **S3.** Style guide per project (formal/informal, honorifics policy,
  gendered-language rules, profanity policy).
- **S4.** Diff-based re-translation: when the user edits a glossary
  entry, automatically re-translate only the affected segments.

### 2.3 Non-Goals (v1)

- **N1.** Translating PDFs. Designed-for, but **not implemented** in v1.
  The architecture must keep the format-handling layer pluggable so PDF
  (and later DOCX, MOBI, AZW3, plain TXT/MD) can be added without
  re-plumbing the pipeline.
- **N2.** Web/desktop GUI. TUI only.
- **N3.** OCR of image-based ePubs.
- **N4.** Hosting LLMs. We are a client.
- **N5.** Crowd/collaborative translation. Single-user, local-first.
- **N6.** Automatic publishing (Kindle, Kobo, etc.).

---

## 3. Personas & User Stories

### 3.1 Personas

- **The Curator (primary).** A bilingual hobbyist or pro translator who
  wants AI to do the heavy lifting but insists on controlling
  terminology and quality. Comfortable in a terminal.
- **The Reader.** Wants a "good-enough" translation of a book that has
  no official version in their language. Will accept light supervision.
- **The Localizer (future).** Pro translator delivering for a publisher;
  needs auditability and glossary export.

### 3.2 Core User Stories

- **U1.** *As a Curator,* I open an ePub, the app extracts every text
  segment, and shows me chapter 1 ready to translate.
- **U2.** *As a Curator,* I translate the first chapter and the app
  proposes glossary entries for every proper noun it detected; I accept,
  reject, or edit each one.
- **U3.** *As a Curator,* I lock the translation of "Élise" → "Elisa".
  In every subsequent chapter, the LLM is forced to use "Elisa" and the
  validator flags any deviation.
- **U4.** *As a Curator,* I batch-translate chapters 2–10 unattended,
  come back, and review only the segments the validator flagged.
- **U5.** *As a Reader,* I quit at chapter 14 and resume next day with
  zero re-work.
- **U6.** *As a Curator,* I rename a character mid-book ("Elisa" →
  "Elise") and the app offers to re-translate every prior segment that
  used the old form.
- **U7.** *As a Curator,* I export a finished `.epub` and a `glossary.json`.
- **U8.** *As a Curator,* I see a running cost meter (USD, tokens) and
  can set a per-project budget cap.

---

## 4. Functional Requirements

### 4.1 ePub I/O & Format Preservation

- **F-IO-1.** Read ePub 2 and ePub 3. Parse OPF, NCX/nav, spine,
  manifest, metadata.
- **F-IO-2.** For each XHTML document in the spine, extract a flat
  ordered list of **translatable text nodes** while preserving a
  reversible mapping back to the DOM. Inline tags (`<em>`, `<strong>`,
  `<a>`, `<ruby>`, footnote markers) must survive.
- **F-IO-3.** Skip non-translatable content: code blocks (`<code>`,
  `<pre>` if marked), `lang`-tagged spans matching the target language,
  user-blacklisted CSS classes, image alt text (configurable).
- **F-IO-4.** **Segmentation.** Split text into translation units no
  larger than a configured token budget (default 800 source tokens),
  preferring sentence and paragraph boundaries. Never split inside an
  inline tag pair.
- **F-IO-5.** **Reassembly.** After translation, splice translated text
  back into the original DOM, preserving inline tags by index. Update
  `<html lang>` and OPF `dc:language` to the target language. Update
  `dc:title` if the title was translated. Add `<meta>` provenance
  (translator, model, date) without breaking validators.
- **F-IO-6.** Write a new ePub with original images, fonts, and CSS
  copied verbatim. Validate with `epubcheck` (warning-only; we do not
  block on errors but surface them).
- **F-IO-7.** **Partial output.** At any point the user can export a
  "preview" ePub where untranslated segments fall back to source text
  (clearly tagged in metadata, optionally visually marked).

### 4.2 Translation Pipeline

For each segment, the pipeline runs these phases:

1. **Extract context.** Pull: previous N segments (translated +
   source), chapter title, book title, project style guide.
2. **Resolve entities.** Match the segment against the glossary (exact
   + alias + optional fuzzy/embedding match). Build a constrained
   "must-use" map: `source_term → required_target_term`.
3. **Detect new entities (optional pre-pass).** A cheap LLM or NER
   model proposes candidate proper nouns missing from the glossary.
   Candidates are queued for curator review **before** translation
   when in interactive mode, or auto-added with `unconfirmed` status
   in batch mode.
4. **Translate.** Single LLM call with: system prompt, style guide,
   context window, glossary constraints (as a structured list the model
   is told to honor), source segment. Output is the target segment plus
   a structured "trace" (which glossary terms were used, any new
   entities encountered).
5. **Validate.** Mechanical checks:
   - Every locked glossary term that appears in the source's detected
     entities appears in the target with the canonical translation.
   - Inline tag count matches (no lost `<em>`).
   - No source-language passages leaked through (heuristic).
   - Length sanity (target/source ratio within configured bounds).
6. **Decide.** If valid → store. If not → either auto-retry with a
   stricter prompt (configurable retry budget), or flag for human review.
7. **Persist.** Store the segment, the trace, the LLM call (tokens,
   cost), and any glossary mutations.

### 4.3 Translation Memory ("Lore Bible")

- **F-LB-1.** Entity types (extensible enum): `character`, `place`,
  `organization`, `event`, `item`, `date_or_time`, `phrase`, `term`,
  `other`.
- **F-LB-2.** Each entry stores: canonical source term, canonical
  target term, type, aliases (source side and target side), gender (if
  applicable, for gendered target languages), notes, status
  (`proposed` / `confirmed` / `locked`), first-seen segment, created/updated
  timestamps, and a free-text rationale.
- **F-LB-3.** **Locked** entries are non-negotiable: validator hard-fails
  any segment that violates them, and the LLM prompt presents them as
  hard constraints.
- **F-LB-4.** **Confirmed** entries are strong defaults: validator warns
  on deviation; LLM prompt presents them as preferences.
- **F-LB-5.** **Proposed** entries are LLM/NER-suggested and not yet
  reviewed; they do not constrain anything but show up in the curator's
  inbox.
- **F-LB-6.** **History.** Every change to a glossary entry is versioned
  with reason and timestamp.
- **F-LB-7.** **Cascade re-translation.** When a confirmed/locked entry
  changes, the app lists every previously translated segment containing
  the old term and offers bulk re-translation.
- **F-LB-8.** **Import/Export.** JSON and CSV. Optional "starter
  glossary" import at project creation.

### 4.4 LLM Integration

- **F-LLM-1.** **OpenAI-compatible only** in v1: configurable
  `base_url`, `api_key`, `model`, optional `organization`. Uses the
  `/v1/chat/completions` schema.
- **F-LLM-2.** Two configurable model slots per project:
  - `translator_model` (high-quality, used for the actual translation).
  - `helper_model` (cheap, used for entity detection, summarization,
    reviewer pass; can be the same as translator_model).
- **F-LLM-3.** **Structured outputs.** When supported by the endpoint
  (JSON mode / tool calling), use it for the translator's "trace" and
  for entity detection. Fall back to robust regex/JSON-recovery parsing.
- **F-LLM-4.** **Token accounting.** Use `tiktoken` (or model-specific
  tokenizer when available) to estimate prompts and enforce context
  limits.
- **F-LLM-5.** **Rate limiting & retry.** Exponential backoff on 429s
  and 5xxs. Configurable concurrency (default 1; user can raise).
- **F-LLM-6.** **Caching.** Deterministic cache key
  `(model, system_prompt_hash, user_prompt_hash, glossary_state_hash)`.
  Cache hits are free and instantaneous.
- **F-LLM-7.** **Cost tracking.** Per-model price table (user-editable);
  every call records prompt/completion tokens and cost.
- **F-LLM-8.** **Budget cap.** Optional per-project hard cap (USD); when
  hit, batch mode pauses and the TUI surfaces a confirmation prompt.

### 4.5 Embeddings (Optional, S1)

- **F-EMB-1.** Pluggable provider (OpenAI-compatible `/v1/embeddings`
  or local sentence-transformers).
- **F-EMB-2.** Embed: every translated segment, every glossary entry
  (source + notes).
- **F-EMB-3.** Use cases:
  1. **Style retrieval:** for each new translation, pull the K most
     similar already-translated segments to seed style consistency.
  2. **Glossary fuzzy match:** detect that "Elise" in the source is
     likely the same entity as the existing "Élise" entry.
  3. **Duplicate-entity detection:** warn the curator before creating a
     near-duplicate glossary entry.
- **F-EMB-4.** Storage in SQLite (BLOB) or `sqlite-vec` extension.

### 4.6 TUI (Textual)

Top-level screens:

- **Projects.** List, create, open, delete, archive.
- **Project Dashboard.** Progress bar (segments translated /
  validated / locked), cost meter, recent activity log, quick actions.
- **Reader.** Side-by-side source / target view, segment-by-segment;
  keybindings to translate-next, accept, edit, reject, lock entities,
  jump-to-chapter.
- **Glossary.** Filterable, sortable table; detail pane with history;
  bulk operations; conflict resolver.
- **Inbox.** Curator queue: proposed glossary entries, validation
  failures, low-confidence translations, budget alerts.
- **Settings.** LLM provider, models, prices, prompt templates, style
  guide, output formatting.
- **Logs.** Tail of the event stream + LLM call log with full
  prompts/responses (collapsible).

UX requirements:

- **Keyboard-first.** Every action has a binding. Mouse optional.
- **Non-blocking.** LLM calls run in workers; the UI never freezes.
- **Theming.** Light/dark; high-contrast variant.
- **Live updates.** Reader view updates as worker finishes a segment.

### 4.7 Persistence & Resume

- **F-P-1.** All state in a single per-project SQLite file
  (`<project>.epublate`). Project assets (original ePub, derived XHTML,
  generated outputs) live next to it in a project folder.
- **F-P-2.** Append-only event log table records every state-changing
  action (for audit and "undo last batch").
- **F-P-3.** Idempotent restart: the app must be safe to crash and
  reopen at any point with no corruption.

### 4.8 Cost & Telemetry

- **F-T-1.** No external telemetry. Anything sent off-machine goes only
  to the LLM/embedding endpoints the user configured.
- **F-T-2.** Local stats: tokens by model, cost by chapter, cache hit
  rate, average segment latency, validation failure rate.

---

## 5. Non-Functional Requirements

- **NFR-1. Local-first.** No mandatory cloud services beyond the LLM
  endpoint. Works fully offline if the user points it at a local model.
- **NFR-2. Cross-platform.** macOS, Linux, Windows (Windows Terminal).
- **NFR-3. Privacy.** Book contents never leave the machine except via
  the user-configured LLM/embedding endpoints. No analytics.
- **NFR-4. Performance.** UI responsive at 60 fps equivalent (no
  perceptible blocking) on a 100k-word book. Batch translation
  throughput limited by LLM, not the app.
- **NFR-5. Determinism.** Same project + same model + same seed +
  same glossary state → same output (insofar as the model supports
  determinism).
- **NFR-6. Reliability.** Crash-safe writes (SQLite WAL mode, atomic
  ePub export to a temp file then rename).
- **NFR-7. Testability.** A `--mock-llm` mode that uses a deterministic
  fake provider for tests and demos. The full test suite must run with
  a single command (`uv run pytest`) on a freshly cloned repo, with no
  manual venv setup, no network, and no LLM keys required.
- **NFR-8. Extensibility.** Format adapters (ePub now, PDF later) and
  LLM providers behind clean interfaces.
- **NFR-9. Tooling.** [`uv`](https://docs.astral.sh/uv/) is the
  canonical tool for environment management, dependency resolution,
  running tests/linters, and building/publishing. Contributors should
  not need `pip`, `pipx`, `poetry`, `pyenv`, `tox`, or `virtualenv`
  installed — only `uv` and a supported Python version (which `uv` can
  itself install).

---

## 6. Technical Architecture

### 6.1 Tech stack (proposed)

- **Language:** Python 3.11+ (pinned via `.python-version`; installed
  by `uv python install` on contributor machines).
- **Project & env tooling:** [`uv`](https://docs.astral.sh/uv/) for
  everything: virtualenv creation, dependency resolution, lockfile
  (`uv.lock`), test/lint runners (`uv run …`), build, publish, and
  end-user installation (`uv tool install epublate`).
- **TUI:** [Textual](https://textual.textualize.io/) + Rich.
- **ePub:** `ebooklib` for container parsing; `lxml` + `BeautifulSoup4`
  for XHTML manipulation; `cssutils` if CSS edits are ever needed.
- **DB:** SQLite via `sqlalchemy` (Core, not necessarily ORM) +
  `alembic` for migrations.
- **LLM client:** the official `openai` Python SDK (it accepts
  `base_url`, so it works against any OpenAI-compatible endpoint).
- **Tokenization:** `tiktoken`.
- **Validation:** `pydantic` for typed configs and structured LLM I/O.
- **Optional NER:** `spacy` (small models) for cheap proper-noun
  pre-detection.
- **Optional embeddings:** OpenAI-compatible endpoint, or
  `sentence-transformers` + `sqlite-vec`.
- **Packaging:** `pyproject.toml` (PEP 621) + `uv.lock`. Buildable with
  `uv build`. Installable for end users via `uv tool install epublate`
  (or `uvx epublate` for ephemeral runs).
- **Testing:** `pytest`, `pytest-textual-snapshot`, `hypothesis` for
  segmentation/reassembly invariants. Run via `uv run pytest`.
- **Lint/format/type:** `ruff` (lint + format), `mypy --strict` on the
  core. Run via `uv run ruff …` and `uv run mypy …`.

### 6.2 Module breakdown

```
epublate/
  app/                  # Textual app, screens, widgets
  core/
    project.py          # Project lifecycle, settings
    segmentation.py     # XHTML <-> segments (reversible)
    pipeline.py         # The translate/validate/persist loop
    validators.py       # Glossary, structure, length validators
    cache.py            # LLM call cache
  formats/
    base.py             # FormatAdapter interface
    epub.py             # ePub adapter (read, write, segment, splice)
    pdf.py              # (placeholder for future)
  llm/
    base.py             # LLMProvider interface
    openai_compat.py    # OpenAI-compatible implementation
    prompts/            # Prompt templates (translator, extractor, reviewer)
  glossary/
    models.py           # Entity, Alias, Revision schemas
    matcher.py          # Exact + alias + fuzzy/embedding match
    enforcer.py         # Constraint construction + post-validation
  embeddings/
    base.py
    openai_compat.py
    local.py            # sentence-transformers
  db/
    schema.py
    migrations/
  cli.py                # `epublate ...` entry points (open, new, export)
```

### 6.3 Development workflow with `uv`

A fresh contributor (or CI runner) needs only `uv` on `PATH`. Everything
below is a single command.

```bash
# Install the right Python (reads .python-version, no system Python needed).
uv python install

# Create the project venv and install all locked dependencies (incl. dev extras).
uv sync --all-extras --dev

# Run the test suite (no LLM keys, no network).
uv run pytest

# Run the test suite with coverage and the full mock-LLM E2E suite.
uv run pytest --cov=epublate --run-e2e

# Lint, format, and type-check (matches CI).
uv run ruff check .
uv run ruff format --check .
uv run mypy src/epublate

# Run the app against a sample project (uses the mock LLM by default).
uv run epublate --mock-llm new tests/fixtures/sample.epub

# Build a wheel + sdist.
uv build

# Publish (release pipeline only).
uv publish
```

End users install with:

```bash
uv tool install epublate          # persistent install
uvx epublate path/to/book.epub    # ephemeral run, no install
```

Conventions:

- **Two dependency groups** in `pyproject.toml`: runtime (`[project]
  dependencies`) and `[dependency-groups] dev` for tests/lint/type/docs.
  Optional extras for `embeddings` (sentence-transformers, sqlite-vec)
  and `ner` (spacy + small model) keep the base install lean.
- **`uv.lock` is committed** to guarantee reproducible test runs across
  contributor machines and CI.
- **No `requirements.txt`, no `setup.py`, no `setup.cfg`.** PEP 621
  metadata in `pyproject.toml` is the single source of truth.
- **CI mirrors the local commands above.** GitHub Actions uses
  `astral-sh/setup-uv`, runs `uv sync --all-extras --dev`, then
  `uv run pytest` / `ruff` / `mypy` — no bespoke install steps.
- **`Makefile` (or `justfile`) is optional** and only as a thin set of
  shortcuts (`make test` → `uv run pytest`); never a place where logic
  lives. The canonical contract is the `uv` invocations.

### 6.4 Data model (sketch)

```sql
-- Project-level
CREATE TABLE project (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_lang TEXT NOT NULL,
  target_lang TEXT NOT NULL,
  source_path TEXT NOT NULL,
  style_guide TEXT,
  budget_usd REAL,
  created_at INTEGER NOT NULL
);

CREATE TABLE chapter (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  spine_idx INTEGER NOT NULL,
  href TEXT NOT NULL,
  title TEXT,
  status TEXT NOT NULL                  -- pending|in_progress|done|locked
);

CREATE TABLE segment (
  id TEXT PRIMARY KEY,
  chapter_id TEXT NOT NULL REFERENCES chapter(id),
  idx INTEGER NOT NULL,
  source_text TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  target_text TEXT,
  status TEXT NOT NULL,                 -- pending|translated|validated|flagged|approved
  inline_skeleton BLOB,                 -- serialized tag positions
  UNIQUE(chapter_id, idx)
);

CREATE TABLE glossary_entry (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  type TEXT NOT NULL,
  source_term TEXT NOT NULL,
  target_term TEXT NOT NULL,
  gender TEXT,
  status TEXT NOT NULL,                 -- proposed|confirmed|locked
  notes TEXT,
  first_seen_segment_id TEXT REFERENCES segment(id),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE glossary_alias (
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL REFERENCES glossary_entry(id),
  side TEXT NOT NULL,                   -- source|target
  text TEXT NOT NULL,
  UNIQUE(entry_id, side, text)
);

CREATE TABLE glossary_revision (
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL REFERENCES glossary_entry(id),
  prev_target_term TEXT,
  new_target_term TEXT,
  reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE entity_mention (
  id TEXT PRIMARY KEY,
  segment_id TEXT NOT NULL REFERENCES segment(id),
  entry_id TEXT NOT NULL REFERENCES glossary_entry(id),
  source_span_start INTEGER,
  source_span_end INTEGER
);

CREATE TABLE llm_call (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES project(id),
  segment_id TEXT REFERENCES segment(id),
  purpose TEXT NOT NULL,                -- translate|extract|review|...
  model TEXT NOT NULL,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost_usd REAL,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  request_json TEXT,
  response_json TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE embedding (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,                  -- segment|glossary_entry
  ref_id TEXT NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL
);

CREATE TABLE event (
  id INTEGER PRIMARY KEY,
  project_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
```

### 6.5 Format adapter interface

```python
class FormatAdapter(Protocol):
    def load(self, path: Path) -> Book: ...
    def iter_chapters(self, book: Book) -> Iterable[ChapterDoc]: ...
    def segment(self, doc: ChapterDoc, max_tokens: int) -> list[Segment]: ...
    def reassemble(
        self, doc: ChapterDoc, translated: list[Segment]
    ) -> ChapterDoc: ...
    def save(self, book: Book, out_path: Path) -> None: ...
```

The ePub adapter implements this. The future PDF adapter implements
the same interface; the rest of the system is format-agnostic.

### 6.6 LLM provider interface

```python
class LLMProvider(Protocol):
    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        response_format: ResponseFormat | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> ChatResult: ...
```

`openai_compat.py` is the only implementation in v1.

---

## 7. Translation Workflow (detailed)

### 7.1 Project creation

1. User runs `epublate new path/to/book.epub`.
2. App copies the ePub into the project folder (immutable original).
3. App parses the ePub, builds chapter and segment rows, computes
   source token counts, estimates cost.
4. User picks source/target language, models, style guide, optional
   starter glossary.
5. App optionally runs a one-shot **"book intake"** helper-LLM call:
   reads the first ~N segments and produces a draft glossary of likely
   characters, places, and the narrative POV/tense. All entries land
   as `proposed`.

### 7.2 Translating a chapter (interactive)

1. User opens the Reader on chapter 1.
2. For each segment in order:
   - App resolves glossary matches for the source.
   - App translates with the translator model.
   - App validates.
   - User sees source / target side by side; can:
     - Accept (status → `approved`).
     - Edit and accept (edit captured as a delta).
     - Reject and request retry (with a free-form note prepended to the
       retry prompt).
     - Promote a detected entity to `confirmed` or `locked`.
3. At chapter end, app summarizes: cost, validator failures, new
   glossary entries, average segment latency.

### 7.3 Translating in batch

1. User selects a chapter range in the Dashboard.
2. App runs the pipeline at configured concurrency.
3. Anything that fails validation lands in the **Inbox** instead of
   blocking the batch.
4. On completion, the Inbox is the curator's worklist.

### 7.4 Glossary curation

- Curator reviews proposed entries: accept, edit, merge into existing,
  or reject.
- Locking an entry triggers a back-check: scan all already-translated
  segments containing the source term; queue mismatches for re-translation.

### 7.5 Re-translation cascade

When a confirmed/locked entry changes target term:

1. Compute affected segments (source contains the source term, OR
   target contains the old target term).
2. Mark them `pending` again (preserving prior translation as history).
3. Curator reviews the count and either runs the cascade or defers.

### 7.6 Export

- **Final ePub.** Walk every chapter, splice approved translations into
  the original DOM, copy assets, write a new ePub.
- **Glossary.** JSON (canonical) + CSV (flat, for spreadsheets).
- **Audit bundle (optional).** Zip of all `llm_call` rows for a given
  chapter range, for reproducibility.

---

## 8. Prompting Strategy (high-level)

### 8.1 Translator prompt skeleton

- **System.** Role, source/target language, style guide, hard
  constraints (locked glossary), soft constraints (confirmed glossary).
- **Context.** Book title, chapter title, the previous K source/target
  segment pairs, optionally the K most similar prior translations
  retrieved by embeddings.
- **User.** The current source segment, with inline tags represented as
  opaque placeholders (`[[T0]]…[[T0]]`) so the model never has to
  reason about HTML.
- **Response format.** JSON: `{ "target": "...", "used_entries": [...],
  "new_entities": [...], "notes": "..." }`.

### 8.2 Extractor prompt (helper model)

Given a chunk of source text, return candidate proper nouns / recurring
terms with type, evidence span, and confidence. Used for the book
intake pass and as a pre-pass in batch mode when entity-detection is
enabled.

### 8.3 Reviewer prompt (optional, S2)

Given source, target, and the glossary slice that should apply, return
a list of issues: `{kind, severity, suggestion}`. Cheap model.

### 8.4 Inline-tag handling

Inline formatting is replaced with placeholders before the LLM call and
restored after. The validator confirms that every placeholder issued in
the source appears exactly once in the target. The model is told this
explicitly and given a short failure-handling instruction.

---

## 9. Risks & Mitigations

| # | Risk                                                   | Likelihood | Impact | Mitigation                                                                                       |
|---|--------------------------------------------------------|------------|--------|--------------------------------------------------------------------------------------------------|
| R1 | LLM ignores glossary constraints                       | High       | High   | Post-translation validator; auto-retry with stricter prompt; flag for human review on repeat fail. |
| R2 | Inline tag corruption (lost `<em>`, broken footnotes)  | Medium     | High   | Placeholder substitution + structural validator; refuse to splice on mismatch.                    |
| R3 | Cost blowout on long books                             | High       | Medium | Token estimate up front; per-project budget cap; aggressive caching; cheaper helper model.       |
| R4 | Drift in proper nouns before glossary is populated     | High       | Medium | Book-intake pre-pass; require curator to confirm a starter glossary before batch mode.           |
| R5 | ePub variance breaks parsing (DRM, exotic structure)   | Medium     | Medium | Refuse DRM'd files explicitly; `epubcheck` warnings surfaced; fallback "raw XHTML" mode.         |
| R6 | Resumability bugs corrupt a project                    | Low        | High   | SQLite WAL; atomic export; nightly auto-snapshot of the project DB.                              |
| R7 | LLM provider outage mid-batch                          | Medium     | Low    | Backoff + retry; partial-batch resume; allow swapping provider mid-project.                      |
| R8 | Style inconsistency across long books                  | High       | Medium | Style-retrieval via embeddings (S1); style guide in system prompt; reviewer pass (S2).           |

---

## 10. Milestones / Roadmap

### M0 — Skeleton (week 1)

- `pyproject.toml` (PEP 621) + `uv.lock`, `.python-version`, package
  layout under `src/epublate/`.
- Bootstrap contract: `uv sync --all-extras --dev && uv run pytest`
  works on a fresh clone with zero other prerequisites.
- CI on GitHub Actions using `astral-sh/setup-uv`: runs `uv sync`,
  `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`. Caches `uv` and `uv.lock` for fast runs.
- Textual "hello world" with Projects screen.
- SQLite schema + migrations + repo layer.
- Mock LLM provider for tests (deterministic, no network).
- `CONTRIBUTING.md` with the canonical `uv` commands.

### M1 — ePub round-trip (week 2)

- ePub adapter: load, iterate chapters, segment, reassemble, save.
- Property-based tests: segment+reassemble == identity on untranslated
  segments.
- CLI: `epublate new`, `epublate export`.

### M2 — Single-segment translation (week 3)

- OpenAI-compatible provider with retry/backoff/cost tracking.
- Translator prompt + JSON response parsing.
- Reader screen with side-by-side view, accept/edit/retry.
- Per-call caching.

### M3 — Glossary v1 (week 4)

- Entity schema, exact + alias matching.
- Glossary screen (table, detail, create/edit/lock).
- Validator: locked-term enforcement, inline-tag count check.
- Cascade re-translation flow.

### M4 — Batch mode + Inbox (week 5)

- Worker pool with concurrency cap.
- Inbox screen for validation failures and proposed entries.
- Cost meter + budget cap.

### M5 — Book intake & extractor (week 6)

- Helper-LLM extractor prompt.
- "First-pass" intake on project creation.
- Pre-pass entity detection in batch mode.

### M6 — Polish & v1 release (week 7)

- Theming, keybinding cheat sheet, snapshot tests.
- Documentation, demo book, packaging on PyPI.
- `epubcheck` integration on export.

### Post-v1

- Embeddings (S1).
- Reviewer/critic loop (S2).
- Style guide DSL (S3).
- PDF adapter.

---

## 11. Open Questions

1. **Segmentation granularity.** Sentence-level gives best
   consistency control but increases call count and latency. Paragraph
   default with a hard token cap is the proposed compromise — does the
   curator want a per-project knob?
2. **Locked terms in dialogue.** If a character calls another by a
   nickname, do we lock the nickname separately or as an alias of the
   canonical entry? Proposal: alias by default, promotable to its own
   entry.
3. **Gendered/pronoun policy** in target languages with grammatical
   gender. Proposal: store `gender` on character entries and pass it as
   a constraint; surface a curator prompt the first time a character
   appears.
4. **Title / front-matter / TOC translation.** Translate by default but
   make it opt-out per item.
5. **`epubcheck` strictness.** Block export on errors, warn on warnings,
   or just log? Proposal: warn-only, with a "strict export" toggle.
6. **Multi-book projects** (a series sharing a glossary). Out of scope
   for v1, but the schema is ready: `project` could become `book` under
   a parent `series`.
7. **License and distribution model.** Already MIT per repo; confirm
   we're happy distributing on PyPI as MIT.

---

## 12. Glossary (of this document)

- **Segment.** The smallest translation unit produced by the format
  adapter. Typically one paragraph; never crosses an inline tag pair.
- **Entry.** A row in the lore bible (a character, a place, etc.).
- **Locked.** Curator-approved, non-negotiable translation choice.
- **Confirmed.** Curator-approved, strong default but overridable with
  warning.
- **Proposed.** Auto-suggested, awaiting curator review.
- **Trace.** The structured side-output of a translator call: which
  glossary entries were honored and what new entities were spotted.
- **Cascade.** Re-translation triggered by a glossary change, scoped to
  segments actually affected by the change.
