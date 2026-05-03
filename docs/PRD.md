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
- **S3.** Style guide DSL — fine-grained, machine-readable rules
  (formal/informal, honorifics policy, gendered-language rules,
  profanity policy). Goal-oriented **tone presets** for v1 are now
  in-scope (see F-STYLE-1/2/3); S3 covers a richer, post-v1 DSL on
  top of the same `project.style_guide` slot.
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
  inline tag pair. **Resolved (M1):** the default unit is the
  paragraph-level block host (`<p>`, `<h1>`-`<h6>`, `<li>`, `<td>`,
  `<th>`, `<figcaption>`, `<blockquote>`, `<dt>`, `<dd>`, `<caption>`).
  Splitting falls back to sentence boundaries (`[.!?]\s+`) only when a
  block exceeds `max_tokens`; the curator can later override the budget
  per project (open question #1 closed).
- **F-IO-5.** **Reassembly.** After translation, splice translated text
  back into the original DOM, preserving inline tags by index. Update
  `<html lang>` and OPF `dc:language` to the target language. Update
  `dc:title` if the title was translated. Add `<meta>` provenance
  (translator, model, date) without breaking validators.
- **F-IO-6.** Write a new ePub with original images, fonts, and CSS
  copied verbatim. The optional `[epubcheck]` extra wires the
  upstream Python wrapper around the official Java JAR; when
  installed, `epublate export --epubcheck` prints a one-line summary
  and `--strict` exits non-zero (3) on any error. When the extra
  isn't installed, both flags emit a "skipped" summary instead of
  crashing — validation is opt-in by design (resolves open
  question #4).
- **F-IO-7.** **Partial output.** At any point the user can export a
  "preview" ePub where untranslated segments fall back to source text
  (clearly tagged in metadata, optionally visually marked).

### 4.2 Translation Pipeline

For each segment, the pipeline runs these phases:

1. **Extract context.** Pull: previous N segments (translated +
   source), chapter title, book title, project style guide.
2. **Resolve entities.** Match the segment against the glossary (exact
   + alias + optional fuzzy/embedding match). Build a constrained
   "must-use" map: `source_term → required_target_term`. Only entries
   whose source term actually hits in this segment (with word-boundary
   matching) are rendered into the LLM's system prompt — passing the
   whole project glossary every time bloats prompts and over-applies
   common-noun mappings (e.g. enforcing `House → Câmara` on plain
   `house` in unrelated prose). Target-only entries (F-LB-9) and
   gender markers (F-LB-2) ride alongside.
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
  hard constraints. **Particle symmetry contract.** Every glossary
  entry — locked, confirmed, *or* proposed — must have *symmetric*
  leading function words: either both `source_term` and `target_term`
  carry a leading article / preposition (`the USA → os EUA`,
  `the United States → os Estados Unidos`), or neither does
  (`Europe → Europa`, `USA → EUA`). Asymmetric pairs like
  `Europe → na Europa` are pathological: the translator dutifully
  inserts the contracted preposition again on the next "in Europe"
  encounter and emits `"na na Europa"`. The contract is enforced in
  three places: (a) the helper-LLM auto-proposer (`upsert_proposed`)
  strips a single leading particle from each side before insert, so
  the noisy proposal path always lands lemma-form pairs;
  (b) `EntryEditScreen` rejects asymmetric saves with a curator-
  facing error so manual edits are forced into one of the two
  acceptable shapes; (c) the runtime validator
  `find_target_doubled_particles` scans the LLM's target text for
  adjacent identical function words and soft-flags the segment for
  curator review (severity `warning`, kind `doubled_particle`,
  surfaced through `has_flagging_violation`). The translator system
  prompt carries the rule in the hard-rules block. Per-language
  particle sets are curated in `epublate.glossary.normalize` for
  `en`, `pt`, `es`, `fr`, `it`, `de`; unknown languages fall back
  to the English set (conservative — better to miss a stripping
  opportunity than aggressively rewrite an unfamiliar tongue).
- **F-LB-4.** **Confirmed** entries are strong defaults: validator warns
  on deviation; LLM prompt presents them as preferences.
- **F-LB-5.** **Proposed** entries are LLM/NER-suggested and not yet
  reviewed; they do not constrain anything but show up in the curator's
  inbox. The auto-proposer accepts a best-effort ``target_term`` from
  the model — the helper-LLM extractor's prompt asks for an idiomatic
  translation of each candidate, and the translator's prompt is
  required to echo back the exact target spelling it used in the
  segment. When a proposed entry already exists with a placeholder
  ``target_term == source_term`` (e.g. seeded from an older intake
  pass that didn't capture target spellings) the next observation
  with a real translation backfills the placeholder. Curator-edited
  rows are never overwritten.
- **F-LB-6.** **History.** Every change to a glossary entry is versioned
  with reason and timestamp. The Glossary screen surfaces a per-entry
  **usage trail** alongside the revision log: the table shows total
  mention count and distinct segment count (`Uses` column), and `o`
  on the highlighted row opens a "Show occurrences" modal that lists
  every recorded use in book order with the matched span wrapped in
  `«…»`. Both views are powered by the existing `entity_mention`
  rows the pipeline writes for every translated segment, so the
  count is exact (no per-row re-scanning of source text) and merges
  preserve it because `entity_mention` rows are repointed to the
  winner.
- **F-LB-7.** **Cascade re-translation.** When a confirmed/locked entry
  changes, the app lists every previously translated segment containing
  the old term and offers bulk re-translation.
- **F-LB-8.** **Import/Export.** JSON and CSV. Optional "starter
  glossary" import at project creation.
- **F-LB-9.** **Target-only entries** (soft-locked). Some entries pin
  a canonical target spelling without ever recording the source-side
  wording — typical when a Lore Book (F-LB-10) is built from an
  already-translated edition rather than the original text. These
  entries set ``source_term=NULL`` and ``source_known=False``. Because
  there is no source pattern to anchor the validator, locked
  target-only entries are **soft-locked**: the validator emits a
  warning (not an error) when the canonical target form is missing,
  and the translator's system prompt renders them in a dedicated
  "canonical target terms used in this work" block that asks the
  model to map source-language references it sees in-segment to the
  canonical target form. Source-keyed locked entries (F-LB-3) keep
  their hard-fail semantics — the relaxation applies only to
  target-only entries.
- **F-LB-10.** **Lore Books and series.** A *Lore Book* is a portable
  Lore Bible stored as its own SQLite-backed folder
  (``<name>.epublate-lore``). It can be created standalone, populated
  from a source-language ePub via intake or from an already-translated
  ePub via target-only extraction (F-LB-9), and then **attached** to
  one or more translation projects. Each project keeps an
  ``attached_lore`` list with a per-attachment mode
  (``read_only`` | ``writable``) and priority. At translate time the
  pipeline merges entries from the project DB and every attached
  Lore Book (own *locked* > attached *locked* > own *confirmed* > …)
  into a single matcher view; the LLM cache key hashes that merged
  state so attaching/detaching invalidates cleanly. New auto-proposed
  entries route to the highest-priority writable Lore Book when one
  exists (else the project's own DB), so a Lore Book accumulates
  bilingual mappings across an entire series. Projects can attach
  any number of Lore Books; Lore Books can be shared across projects.
  **Bootstrapping from a translated book.** A Lore Book can also be
  populated directly from an existing translation project's curated
  glossary — ``epublate lore new --from-project <path>`` and
  ``epublate lore import-project <lore> <project>`` (CLI, ``--on-conflict
  skip|overwrite``) and the Dashboard's ``p`` keybinding (TUI, with
  per-conflict resolution). This is the fast path for series whose
  book one was translated before the curator decided to factor a
  Lore Book out: the destination accumulates source/target term
  mappings with aliases, gender, notes, and source-known flags from
  the project, and source-keyed conflicts are resolved by the curator
  (``keep_existing`` / ``use_incoming`` / ``skip``). Re-importing the
  same project is a no-op for unchanged rows.
  Resolves PRD §11 open question #4.

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
  every call records prompt/completion tokens and cost. The price
  lookup falls back through ``model`` → ``model`` minus the
  ``-YYYY-MM-DD`` snapshot suffix → longest-prefix-match (so
  ``gpt-4o-2024-08-06`` and ``gpt-4o-mini-2024-07-18`` resolve to
  their families without bespoke entries). The Dashboard cost panel
  surfaces running spend, prompt + completion token totals,
  cache-hit rate, and call count; a dedicated **LLM activity** screen
  (``L`` from the Dashboard) drills into per-model and per-purpose
  rollups plus a recent-calls audit table.
- **F-LLM-8.** **Budget cap.** Optional per-project hard cap (USD); when
  hit, batch mode pauses and the TUI surfaces a confirmation prompt.
- **F-LLM-9.** **Small-segment grouping.** Dense list-like content
  (table of contents, indices, glossaries) is translated in batched
  LLM calls instead of one round-trip per segment. The pipeline only
  groups segments that are ``pending``, short (default ≤240 chars),
  and lightly-marked-up — up to ``group_max_placeholders`` (default
  8) inline-tag placeholders, so a TOC link such as ``<li><a href="..
  ">Chapter 1</a></li>`` (one ``[[T0]]…[[/T0]]`` pair) groups
  alongside plain ``<li>`` entries. Heavier markup falls back to the
  per-segment path where the richer prompt context is the safer call.
  Grouping never crosses a chapter boundary. Each segment still gets
  its own ``llm_call`` audit row (tokens and cost allocated
  proportionally to completion length) and its own cache key, so a
  second run finds cache hits individually. On group-parse failure
  the pipeline falls back to per-segment translation so a bad batch
  can never corrupt a segment. Default group size is 50 items;
  user-configurable via the batch modal and the CLI
  (`--group-small` / `--group-max-items`).
- **F-LLM-10.** **Trivial-segment short-circuit.** Segments whose
  source is wholly placeholders plus invisible glue characters
  (``&nbsp;`` / ``\u00a0``, BOM, zero-width spaces / joiners) skip
  the LLM entirely: the pipeline copies ``source_text`` to
  ``target_text``, marks the segment ``translated``, and emits a
  ``segment.translated_trivial`` event. **No** ``llm_call`` row is
  written because no call was made (this is the one carve-out from
  "every translation has an llm_call row"). The intake-time
  segmenter already drops these on ingest; the pipeline-level check
  is defense-in-depth so projects segmented before this filter
  existed (``<p>&#160;</p>`` separators from Calibre-converted
  ePubs) also avoid spurious round-trips. Segments with one
  character of real content (a chapter number "1", a dash "—") are
  *not* trivial and still go through the translator.

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
  jump-to-chapter. The two panes are scroll-synced segment-by-segment
  (an offset within a source card maps to the same fractional offset
  on the matching target card) so the curator can skim either side
  and the other follows in lockstep, even when the target is shorter
  or longer than the source.
- **Glossary.** Filterable, sortable table; detail pane with history;
  bulk operations; conflict resolver.
- **Inbox.** Curator queue: proposed glossary entries, validation
  failures, low-confidence translations, budget alerts.
- **Settings.** LLM provider, models, prices, prompt templates, style
  guide, output formatting.
- **Logs.** Unified tail of three streams (`app/screens/logs.py`):
  the project ``event`` table, the in-process Python ring-buffer
  (`app/log_buffer.RingBufferHandler`), and the ``llm_call`` table.
  Source / level / time / substring filters; row highlight expands
  the full payload (event JSON, log traceback, ``llm_call`` request
  + response). Bound to lowercase ``l`` from the Dashboard
  (uppercase ``L`` keeps the cost-focused LLM-activity view).

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

### 4.9 Tone & Style Presets

Translation quality collapses when the LLM has no signal about the
register the curator wants — children's stories come out solemn,
explicit fiction comes out sanitized, technical manuals come out
flowery. The translator's system prompt has always carried a free-form
``style_guide`` string (see §8.1); these requirements give curators a
two-second way to populate it well.

- **F-STYLE-1.** **Tone preset registry.** Ship a small catalog of
  named **style profiles** (literary fiction, children's picture
  book, middle grade, young adult, genre fiction, cozy romance,
  explicit adult, technical manual, academic, journalistic). Each
  profile expands to a pre-written paragraph the translator's system
  prompt embeds verbatim (`epublate.core.style.StyleProfile`).
  Resolution rules:
  - The New Project flow (modal + `epublate new --style-profile`) lets
    the curator pick a preset (default: ``literary_fiction``), pick
    "None" to opt out, or author free-form prose that overrides the
    preset's text.
  - The chosen preset slug lands in `project.style_profile`; the
    resolved prompt prose lands in `project.style_guide` (see §6.4).
    Both columns ride through the existing translator system-prompt
    hash, so a preset switch correctly invalidates cached
    translations (F-LLM-6).
  - The `explicit_adult` preset is shipped because the app is
    local-first (NFR-3): book contents only leave the machine via the
    user-configured endpoint. The dashboard and Settings screen
    surface the active preset so the choice is always visible.
- **F-STYLE-2.** **Edit later.** The Settings screen (§4.6) renders a
  Style guide panel (active preset + a short prompt-prose preview)
  and binds `E` to open a modal that re-uses the New Project tone
  field (preset Select + editable TextArea). Save persists via
  `Project.update_style` and records a `project.style_changed` event
  on the audit log; the next batch picks up the new system prompt
  automatically.
- **F-STYLE-3.** **Helper-LLM co-proposal.** The intake / pre-pass
  helper (§7.1 step 5, §4.2 step 3) is asked to surface two
  best-effort style observations alongside its glossary candidates:
  ``register`` (literary / genre / romance / explicit / technical /
  academic / journalistic / neutral) and ``audience`` (children /
  middle_grade / young_adult / adult / general). The intake summary
  carries them through and resolves a `suggested_style_profile` via
  `epublate.core.style.suggest_style_profile`; the CLI prints the
  suggestion (and the dashboard surfaces "Helper suggests: …" when
  it differs from the active preset). The suggestion is never
  applied automatically — the curator owns the choice.
- **F-STYLE-4.** **Pre-create tone sniff.** The New Project modal
  (§4.6) auto-detects the right preset *before* the curator hits
  Create. When the source ePub settles (Browse pick or Enter on the
  source field), a background worker calls
  `epublate.core.style_sniff.sniff_tone` — it reads a head/middle/tail
  spread of translatable blocks (default 5/3/2, capped at 12k chars),
  asks the helper LLM the same `(register, audience)` question §8.2
  uses, and runs the answer through `suggest_style_profile`. The Tone
  Select pre-populates with the suggestion (or stays put if the
  curator manually picked first); the status row always names the
  helper's verdict and per-call cost. Hard rules:
  - **Toggle** — `UIConfig.auto_tone_sniff` defaults `True`. The
    Settings screen binds `A` to flip + persist it. The
    `EPUBLATE_AUTO_TONE_SNIFF` env var (1/true/yes/on or 0/false/no/off)
    overrides the persisted bool at runtime so curators can pin
    behavior in CI / scripted runs.
  - **Privacy / cost** — the sniff runs the *user-configured* helper
    endpoint (NFR-3); we never reach a third party. Failures (no API
    key, parse error, malformed ePub) reduce to a one-line italic
    status — the modal stays usable. No persistence: nothing about
    the sniff lands in the project DB.
  - **Caching** — none in v1. Re-picking the same source path
    short-circuits via in-modal debounce; relaunching the modal is
    a fresh call. (Caching by ePub-content hash is a future PR if
    cost becomes an issue.)
  - **Override** — the helper never overwrites a manual tone pick.
    The status row still names the suggestion so the curator can
    apply it from the Tone Select.

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

- **Language:** Python 3.13+ (pinned via `.python-version`; installed
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
  style_guide TEXT,                     -- resolved tone-preset prose (F-STYLE-1)
  style_profile TEXT,                   -- preset slug, NULL ⇒ "None" / custom (F-STYLE-1)
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

The helper is also asked for two best-effort style tags (F-STYLE-3):
``register`` (literary / genre / romance / explicit / technical /
academic / journalistic / neutral) and ``audience`` (children /
middle_grade / young_adult / adult / general). Both are nullable;
``epublate.core.style.suggest_style_profile`` maps the resulting pair
to a tone-preset id surfaced on the dashboard / CLI.

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

### M1 — ePub round-trip (week 2) ✅

- [x] ePub adapter: load, iterate chapters, segment, reassemble, save.
- [x] Property-based tests: segment+reassemble == identity on
  untranslated segments.
- [x] CLI: `epublate new`, `epublate export`.

### M2 — Single-segment translation (week 3) ✅

- [x] OpenAI-compatible provider with retry/backoff/cost tracking.
- [x] Translator prompt + JSON response parsing.
- [x] Reader screen with side-by-side view, accept/edit/retry.
- [x] Per-call caching.

### M3 — Glossary v1 (week 4) ✅

- [x] Entity schema, exact + alias matching.
- [x] Glossary screen (table, detail, create/edit/lock).
- [x] Validator: locked-term enforcement, inline-tag count check.
- [x] Cascade re-translation flow.
- [x] JSON import/export (`epublate glossary export|import`,
  `epublate new --starter-glossary FILE`).
- [x] Auto-propose `proposed` entries from the translator's
  `trace.new_entities`.

### M4 — Batch mode + Inbox (week 5) ✅

- [x] Worker pool with concurrency cap (`core.batch.run_batch`,
  `ThreadPoolExecutor`-driven, per-segment failures recorded as
  `batch.segment_failed` events without aborting the run).
- [x] Inbox screen for validation failures, proposed glossary entries,
  and curator alerts (`InboxScreen` + `epublate inbox`).
- [x] Cost meter + budget cap (`CostMeter` widget, `core.stats`,
  `BudgetModal`, `epublate budget set|clear|show`); cumulative spend
  trips `BatchPaused` and emits `batch.paused` for the Inbox.
- [x] Project Dashboard becomes the new landing screen for
  `epublate open`, with bindings to Reader (`o`), Glossary (`g`),
  Inbox (`i`), Batch (`b`), Budget (`B`), and Refresh (`r`).

### M5 — Book intake & extractor (week 6) — landed

- [x] Helper-LLM extractor prompt (`llm/prompts/extractor.py` —
  `ExtractedEntity` / `ExtractorTrace`, `build_extractor_messages`,
  `parse_extractor_response`).
- [x] "First-pass" intake on project creation (`core/extractor.py` —
  `run_book_intake`, opt-in via `epublate new --intake` and
  `epublate intake`; emits `intake.started` / `intake.completed`
  events and seeds `proposed` glossary entries).
- [x] Pre-pass entity detection in batch mode (`core/batch.py` —
  `BatchOptions.pre_pass`, opt-in via `epublate batch --extract`;
  per-chapter helper call before translator futures fire, emits
  `batch.pre_pass_started` / `batch.pre_pass_completed`).
- [x] Helper model resolution (`llm/factory.py` —
  `EPUBLATE_LLM_HELPER_MODEL`, `resolve_helper_model`, plus
  `--helper-model` flags on `new`, `intake`, and `batch`).
- [x] Dashboard `e` binding + `IntakeModal` + worker
  (`app/screens/dashboard.py`); intake / pre-pass alerts surface
  through `core.stats.ALERT_KINDS` and the Inbox.

### M6 — Polish & v1 release (week 7) ✅

- [x] Theming (high-contrast variant + `T` cycler, persisted to
  `~/.config/epublate/ui.toml`), global cheat-sheet modal on
  `?` / `f1`, first wave of `pytest-textual-snapshot` baselines for
  the Dashboard / Reader / Glossary / Inbox / Help / Settings.
- [x] Read-only Settings screen (`s` from the Dashboard) surfacing
  the LLM env-var-driven config with a redacted API key, the
  active / saved theme, and the project's budget cap.
- [x] Documentation refresh: `README.md`, walkthrough at
  `docs/USAGE.md`, maintainer runbook at `docs/RELEASE.md`,
  `CHANGELOG.md`, packaging metadata bumped to `0.1.0` /
  `Development Status :: 4 - Beta`.
- [x] Optional `epubcheck` integration via the `[epubcheck]` extra
  (`uv sync --extra epubcheck`); `epublate export --epubcheck`
  prints a warn-only summary, `--strict` exits non-zero on errors
  (resolves open question #4 — see F-IO-6).
- [x] Tag-triggered release pipeline (`.github/workflows/release.yml`)
  gated on `PYPI_API_TOKEN`; `build` job in CI ensures `uv build`
  stays green on every PR.

### M7 — Tone presets & style co-proposal — landed

- [x] `epublate.core.style` — preset registry (10 shipped profiles),
  `resolve_style_guide` / `suggest_style_profile` helpers, and
  `DEFAULT_STYLE_PROFILE = literary_fiction` (F-STYLE-1).
- [x] `project.style_profile` column (Alembic migration
  ``0003_project_style_profile``); `Project.create` /
  `Project.update_style` / `epublate new --style-profile|--style-text`
  thread the preset through the schema, repo, and pipeline.
- [x] New Project modal + CLI gain a Tone field; the preset's prose
  is editable inline so the curator sees what the LLM will get.
- [x] Settings screen renders a Style guide panel + `E` →
  `StyleEditModal` that re-uses the same field; `Project.update_style`
  records a `project.style_changed` audit event (F-STYLE-2).
- [x] Helper-LLM extractor prompt + parser surface ``register`` and
  ``audience``; `IntakeSummary.suggested_style_profile` carries the
  suggester's verdict; `epublate intake` and `epublate new --intake`
  print the suggestion (F-STYLE-3).
- [x] **Pre-create tone sniff** in the New Project modal:
  `epublate.core.style_sniff.sniff_tone` reads a head/middle/tail
  spread of the picked ePub and pre-selects the right preset before
  the curator hits Create. Toggle persists in `UIConfig.auto_tone_sniff`
  (default on); `EPUBLATE_AUTO_TONE_SNIFF` overrides at runtime;
  Settings screen binds `A` to flip + persist the toggle (F-STYLE-4).

### M8 — Observability & UX polish — landed

- [x] **Logs screen** (`app/screens/logs.py`) merging events, the
  in-memory Python `RingBufferHandler` (`app/log_buffer.py`), and
  ``llm_call`` rows; source/level/time/substring filters; row
  detail pane; lowercase `l` from the Dashboard. Resolves the
  §4.6 "Logs" line that was specced in M0 but never built.
- [x] **App-wide toast notifications** (`app.notify`) for batch
  failures, pauses, cancellations, completed-with-flags, and
  Reader pipeline failures so errors are visible regardless of
  the focused screen.
- [x] **Batch progress always re-attaches.** `DashboardScreen`
  re-binds itself to the App-level batch listener on `on_mount`
  when `EpublateApp.batch_progress.active` is true for the
  current project; `BatchStatusBar` polished (solid backgrounds,
  `▶ BATCH` ribbon prefix) and mounted on every screen.
- [x] **Modal submit unification.** Every form modal accepts
  ``Enter`` *and* ``Ctrl+S``; `EditTargetScreen` documented as
  the only ``Ctrl+S``-only exception. New rule
  `.cursor/rules/tui-keybindings.mdc` codifies the convention.
- [x] **Glossary quality.** Tighter extractor prompts + parser
  caps (10 words / 100 chars / no full sentences / no
  unbalanced parens), canonical-form dedup in `upsert_proposed`
  (`glossary/dedup.py:canonical_form`), and a unified
  "Cleanup dupes" flow on the Glossary screen wired through the
  existing `MergeDuplicatesScreen` for curator-confirmed merges
  (`glossary/dedup.py:find_near_duplicates`).

### Post-v1

- Embeddings (S1).
- Reviewer/critic loop (S2).
- Style guide DSL (S3) — fine-grained, machine-readable rules on
  top of the v1 tone-preset prose (post-v1; F-STYLE-1/2/3 ship the
  goal-oriented presets in v1).
- PDF adapter.

---

## 11. Open Questions

1. **Locked terms in dialogue.** If a character calls another by a
   nickname, do we lock the nickname separately or as an alias of the
   canonical entry? Proposal: alias by default, promotable to its own
   entry.
2. **Title / front-matter / TOC translation.** Translate by default but
   make it opt-out per item.
3. **License and distribution model.** Already MIT per repo; confirm
   we're happy distributing on PyPI as MIT.

### Resolved

- ~~**Gendered/pronoun policy** in target languages with grammatical
  gender.~~ Resolved: ``gender`` lives on every glossary entry
  (``GenderTag``: ``feminine`` / ``masculine`` / ``neuter`` /
  ``common`` / ``unspecified``). The translator system prompt
  surfaces it inline next to the target term —
  ``[organization] House → Câmara (gender: feminine)`` — and a hard
  rule asks the model to match articles, demonstratives, possessives,
  adjectives, participles, and any preposition contractions to that
  gender. When the source uses a glossary term with an article, the
  translation must keep the article and inflect it correctly. Curator
  prompting on first appearance is post-v1; the auto-extractor's
  best-effort target spelling and the inline gender marker cover the
  common case.

- ~~**Multi-book projects** (a series sharing a glossary).~~ Resolved:
  shipped as **Lore Books** (PRD F-LB-10). Rather than turning
  ``project`` into ``book`` under a parent ``series`` we kept the
  project shape and introduced an attachable Lore Book artifact —
  same outcome (one shared lore corpus across many books), simpler
  schema, and the Lore Book is portable so it can travel with the
  curator across machines or be shared between projects. Target-only
  entries (F-LB-9) make the workflow viable even for series where
  the curator only has the translated editions on hand.
- ~~**`epubcheck` strictness** (M6).~~ Resolved: opt-in via the
  `[epubcheck]` PyPI extra, warn-only by default, `epublate export
  --strict` toggles hard-fail mode (PRD F-IO-6).
- ~~**Style guide shape** (originally Stretch S3).~~ Resolved (M7):
  v1 ships **goal-oriented tone presets** (F-STYLE-1) — short,
  curator-pickable named profiles that expand to a paragraph in
  the translator's system prompt, plus an editable TextArea for
  curators who want full control. The richer machine-readable DSL
  framing of S3 stays post-v1 and will sit on top of the same
  `project.style_guide` column.

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
