# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
