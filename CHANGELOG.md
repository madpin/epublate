# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — App-owned batch lifecycle, cancellation, richer batch UI

- **Batch worker lives on the App, not the Dashboard.** Pressing
  ``b`` on the Dashboard dispatches a batch run that survives
  popping back to the Projects screen (or even opening a different
  project). The originating ``Project`` is held by the App until
  the worker drains, so the SQLite engine stays alive while the
  background batch progresses (PRD §4.6 / §7.3). The Dashboard
  re-attaches as a listener while it's mounted and reads the
  shared ``app.batch_progress`` snapshot otherwise.
- **Cancel binding.** A new ``c`` binding on the Dashboard
  (``EpublateApp.cancel_batch``) signals the worker to stop
  submitting new segments while letting in-flight LLM calls
  complete cleanly. Cancellation is propagated through
  ``core.batch.run_batch`` via a ``threading.Event`` and a fresh
  ``BatchCancelled`` exception that records ``batch.cancelled`` in
  the project's event log.
- **Richer batch progress meter** (PRD §4.6 / batch UI). The
  Dashboard panel now shows the chapter count alongside the
  segment count (``across N chapters · M segments queued``), an
  explicit state badge (``running`` / ``cancelling…`` / ``paused``
  / ``cancelled`` / ``done``) and a two-token cost line —
  ``this batch $X · project total $Y`` — so curators can tell at
  a glance how much the running batch added on top of the
  project's pre-batch spend. The Reader's footer mirror reads the
  same App-level snapshot.
- **Persistent ``BatchStatusBar`` on every main screen.** A new
  slim banner (``src/epublate/app/widgets/batch_status_bar.py``)
  is mounted on the Glossary, Inbox, Settings, LLM Activity, and
  Projects screens. It self-hides while no batch is running and
  rehydrates automatically (500 ms tick) when the App's
  ``batch_progress`` slot flips active. Color-coded for the four
  active states (running / cancelling / paused / cancelled), it
  surfaces the same state badge + counters + cost line as the
  Dashboard panel in a single line, so the curator can wander
  through the inbox or settings without ever losing sight of an
  in-flight run.

### Fixed — Save ePub crash on named entities, Projects "delete"

- **"Save ePub" no longer crashes on named HTML entities**
  (``&nbsp;``, ``&amp;``, ``&copy;``, …) coming out of older ePubs
  that ship an XHTML DTD reference. ``lxml`` exposes those as
  ``etree.Entity`` nodes whose ``tag`` is a ``cyfunction`` rather
  than a string; the segmenter now recognizes that node kind, the
  ``InlineToken`` schema gained a third ``"entity"`` flavor, and
  the reassembler reconstructs the entity verbatim. Legacy
  ``cyfunction Entity at 0x…`` strings persisted by older versions
  are skipped on read so existing projects can re-export without
  manual repair (PRD §7.4 invariant 1 / format preservation).
- **Confirmation dialogs on the Projects screen.** The existing
  ``delete`` / ``x`` "remove from recents" action now goes through
  a ``Y/N`` confirmation modal (project files are kept). A new
  destructive ``D`` action permanently removes the project folder
  and SQLite database after a typed-name confirmation, so a
  fat-fingered keystroke can never wipe a project's state.

### Fixed — Reader scroll flicker, glossary edit modal crash

- **Reader source/target scroll-sync no longer flickers** (PRD §4.6).
  The earlier mirror cleared its sync guard synchronously after
  ``scroll_to``, but Textual's layout pass fires ``scroll_y`` watchers
  asynchronously *after* the call returns; the post-clamp watcher
  saw the guard cleared, treated the rebound as a fresh user scroll,
  and ping-ponged the panes non-stop. The guard now stays set until
  ``call_after_refresh`` releases it (i.e. once layout has settled),
  and a 1-cell convergence guard skips the mirror entirely when the
  destination pane is already aligned.
- **Glossary edit modal crash** on the Gender select. Newer Textual
  releases moved the blank sentinel from ``Select.BLANK`` (which now
  inherits ``Widget.BLANK == False``, the boolean) to ``Select.NULL``
  (a ``NoSelection`` instance). The edit modal was passing the old
  constant, which Textual's value validator rejected with
  ``InvalidSelectValueError: Illegal select value False`` on mount.
  The Settings screen's lore-mode select had the same latent bug
  and is updated alongside.

### Added — Reader scroll-sync, LLM activity screen, richer cost meter

- **Reader source/target scroll sync** (PRD §4.6): scrolling either
  pane mirrors the other segment-by-segment with a fractional offset
  so cards of different rendered heights stay aligned. The mirror
  uses a guard flag to avoid feedback loops with the existing
  highlight-on-navigate scroll.
- **Dedicated `LLMActivityScreen`** (PRD F-LLM-7) reachable with
  ``L`` from the Dashboard. Surfaces totals (calls, cache hits,
  prompt / completion tokens, cost, budget), per-model and
  per-purpose rollups with a fraction-of-spend bar, and a recent
  calls audit table (timestamp, model, purpose, segment, tokens,
  cost, cache flag).
- **`CostMeter` token / call breakdown** (PRD F-LLM-7): the
  Dashboard and Inbox cost panels now render a second + third line
  with input/output token totals (compact ``1.2K`` / ``3.4M``
  format) and call/cache-hit counts. Token totals come from the
  API's ``usage`` block when present and fall back to ``tiktoken``
  when the endpoint omits it (e.g. some Ollama / llama.cpp builds).
- **Forgiving price lookup** (PRD F-LLM-7): ``get_price`` walks
  ``model`` → strip ``-YYYY-MM-DD`` snapshot suffix → longest
  registered prefix, so ``gpt-4o-2024-08-06`` and
  ``gpt-4o-mini-2024-07-18`` resolve to their family pricing
  without bespoke entries. Default table refreshed with the
  GPT-5 / GPT-4.1 / o-series families.
- **Dashboard Progress panel** picked up a consistent ``panel-title``
  header to match the Cost / Inbox / Glossary / LLM activity panels;
  the body now renders a 20-cell ASCII fraction bar so progress is
  scannable at a glance.

### Changed — Glossary auto-proposer learns the actual translation

- **Source-mirror placeholders are gone** (PRD F-LB-5). The
  helper-LLM extractor's prompt now asks for a best-effort
  ``target`` translation per candidate, and the translator's prompt
  is required to echo back the exact target spelling it used inside
  the segment for every entry in ``new_entities``. The auto-proposer
  threads the target through ``glossary_io.upsert_proposed`` so a
  fresh proposal lands with the real translation; existing rows
  whose ``target_term`` still mirrors ``source_term`` are
  backfilled on the next sighting. Curator-edited entries
  (``status != 'proposed'`` or ``target_term`` already differing
  from ``source_term``) are never overwritten. Fixes the
  "``Julius Caesar`` → ``Julius Caesar``" cold-start where the
  translator was using ``Júlio César`` but the glossary still
  showed the source verbatim.
- **Compound / hyphenated terms surface in intake.** The extractor
  prompt now explicitly invites hyphenated compounds
  (``boot-lickers``), multi-word in-world phrases
  (``Council of Five``), and recurring slang / idiomatic insults so
  the helper LLM stops collapsing them into single-token cousins.
  The matcher already handled hyphenated entries correctly; a new
  regression test (``test_glossary_matcher.py``) pins the contract.

### Changed — Defaults & Batch UX

- **Default target language is ``pt-BR``** (Brazilian Portuguese)
  in the New Project modal, matching the most common project shape
  for the project's curator. Override with any BCP-47 code as
  before.
- **Batch modal: force-retranslate option.** A new ``Force
  retranslate cached [y/n]`` field maps to
  ``BatchOptions.bypass_cache``. The default is ``n`` so cache hits
  remain free and instantaneous (PRD F-LLM-6); flip to ``y`` to
  refresh every segment after a glossary cascade or a model swap.

### Changed — Faster batch translation on TOC / index / blank segments

- **TOC / index links group too** (PRD F-LLM-9): the small-segment
  grouping path no longer rejects every segment containing inline-tag
  placeholders. ``is_group_eligible`` now allows up to
  ``GROUP_DEFAULT_MAX_PLACEHOLDERS`` (default 8) placeholders per item
  and ``BatchOptions.group_max_placeholders`` exposes the knob, so a
  table of contents of ``<li><a href="...">Chapter N</a></li>`` entries
  collapses into one LLM round-trip instead of N round-trips. The
  group translator prompt was updated to spell out that placeholder
  ids are local to each item; the existing per-item validation +
  per-segment fallback already kept malformed responses from
  corrupting the segment table.
- **Trivial-segment short-circuit** (PRD F-LLM-10): segments whose
  source is wholly placeholders + invisible glue characters
  (``&nbsp;``, BOM, zero-width spaces / joiners) skip the LLM
  entirely. ``translate_segment`` copies ``source_text`` to
  ``target_text``, emits a ``segment.translated_trivial`` event, and
  doesn't write an ``llm_call`` row (no call was made). The
  intake-time segmenter (``EpubAdapter.segment``) uses the same
  ``epublate.core.segmentation.is_trivially_empty`` helper so new
  projects never accumulate ``<p>&#160;</p>``-only segments either —
  this catches the Calibre separator pattern that previously cost one
  round-trip per blank paragraph.

### Added — M7: Tone presets & style co-proposal (PRD F-STYLE-1/2/3/4)

- **Style profile registry** (`epublate.core.style`): nineteen shipped
  tone presets spanning literary fiction (literary, classic,
  historical), audience-graded fiction (children's picture book,
  middle grade, young adult, fairytale / folklore), adult genre (genre
  fiction, noir / hard-boiled crime, horror / gothic, cozy romance,
  explicit adult), cross-cutting registers (humor / comedy, memoir /
  biography), and specialty registers (poetry / verse, religious /
  spiritual, technical manual, academic, journalistic). Each preset
  expands to a vetted paragraph the translator's system prompt embeds
  verbatim, so the LLM always knows the audience and register.
- **Project schema:** new `project.style_profile` column (Alembic
  migration ``0003_project_style_profile``) carrying the chosen
  preset slug alongside the existing ``style_guide`` prose. Both
  rides through the cache key, so swapping a preset correctly
  invalidates stale translations (PRD F-LLM-6).
- **`epublate new`:** `--style-profile` (default ``literary_fiction``,
  ``list`` to print the catalog, ``none`` to opt out) and
  `--style-text` for free-form prose.
- **TUI:** New Project modal grew a Tone preset Select + an editable
  prompt-block TextArea; the Settings screen shows the active preset
  with a short prose preview and binds ``E`` to a `StyleEditModal`
  for swapping presets / authoring custom prose. Saves emit a
  ``project.style_changed`` audit event.
- **Helper-LLM co-proposal:** the extractor / intake helper is asked
  to surface ``register`` and ``audience`` observations alongside
  its glossary candidates; ``IntakeSummary.suggested_style_profile``
  carries the resolved preset id, and ``epublate new --intake`` /
  ``epublate intake`` print the suggestion when it differs from the
  active tone.
- **Pre-create tone sniff** (PRD F-STYLE-4): new
  ``epublate.core.style_sniff.sniff_tone`` reads a head/middle/tail
  spread of the picked ePub *before* project creation and asks the
  helper LLM the same ``(register, audience)`` question, pre-selecting
  the matching preset in the New Project modal so the curator can hit
  Create with the right voice already loaded. Manual picks are never
  overridden; failures collapse to a one-line italic status.
- **Auto tone-sniff toggle:** ``UIConfig.auto_tone_sniff`` (default on)
  persists to ``~/.config/epublate/ui.toml``; the Settings screen
  binds ``A`` to flip + persist the toggle. The
  ``EPUBLATE_AUTO_TONE_SNIFF`` env var (1/true/yes/on or 0/false/no/off)
  overrides the persisted bool at runtime so curators can pin
  behavior in CI / scripted runs.

## [0.1.0] — 2026-04-30

First publishable cut of `epublate`. All milestones from the PRD
roadmap (M0 through M6) are landed; this is the v1 release candidate.

### Added

- **M0** — `uv`-driven project skeleton, CI matrix on Linux/macOS/Windows,
  agent guides (`AGENTS.md` + symlinked `CLAUDE.md`), and the M0 boot
  smoke test.
- **M1** — ePub 2/3 round-trip via `formats.epub.EpubAdapter` with the
  placeholder-based segmentation that keeps inline tags out of the LLM
  prompt; atomic write-temp-then-rename export; SQLite schema + Alembic
  migrations.
- **M2** — Single-segment translation flow: OpenAI-compatible
  `LLMProvider`, deterministic mock provider for tests, the Reader
  screen with retry / accept / edit, per-call cache keyed on
  `(model, system_prompt_hash, user_prompt_hash, glossary_state_hash)`.
- **M3** — Glossary v1: matcher, validator, cascade re-translation, and
  the helper-LLM auto-proposer wired into the translator's structured
  output schema (PRD F-G-1 through F-G-7).
- **M4** — Project Dashboard, batch mode with concurrency + budget cap,
  curator Inbox surfacing flagged segments / proposed entries / alerts,
  and the cost meter widget with running spend.
- **M5** — Helper-LLM book intake on `epublate new --intake` and an
  optional pre-pass on `epublate batch --extract`; both share a single
  `helper_model` resolved via `EPUBLATE_LLM_HELPER_MODEL` (or the
  translator model as a fallback) and obey the project budget cap.
- **M6 — Polish & v1 release**:
  - High-contrast theme registered alongside Textual's built-ins, plus
    a tri-state cycler bound to `T` that persists the choice in
    `~/.config/epublate/ui.toml` (XDG-style).
  - Global cheat-sheet modal (`?` / `f1`) that introspects the active
    screen's bindings so it stays accurate as the UI evolves.
  - Read-only `SettingsScreen` reachable from the Dashboard with `s`,
    showing the project metadata, the LLM env-var-driven config (with
    a redacted API key), and the active / persisted theme.
  - First wave of `pytest-textual-snapshot` SVG baselines for
    Dashboard, Reader, Glossary, Inbox, Help, and Settings (terminal
    pinned at 120×36) — future PRs that touch UI now flag visual
    diffs by default.
  - Optional `epubcheck` integration via the new `[epubcheck]` extra
    (PRD F-IO-6, resolves open question #4): warn-only by default,
    `epublate export --strict` exits non-zero on errors. The
    integration is graceful — no extra installed or no Java present
    prints a "skipped" summary instead of crashing.
  - Documentation: walkthrough at [`docs/USAGE.md`](docs/USAGE.md),
    maintainer release runbook at [`docs/RELEASE.md`](docs/RELEASE.md),
    refreshed README, and this CHANGELOG.

### Changed

- `pyproject.toml` development status bumped to `4 - Beta`; package
  `version` to `0.1.0`.
- `epublate export` now exposes both `--epubcheck` (warn-only) and
  `--strict` (errors → exit 3) flags. The previous `--strict`
  placeholder is now wired to the `epubcheck` runner.
- The TUI's `d` binding (light/dark toggle) is preserved as a shim;
  the canonical theme action is the `T` cycler.

### Removed

- Nothing — this is the first tagged release.

[0.1.0]: https://github.com/madpin/epublate/releases/tag/v0.1.0
