# Using epublate (curator's walkthrough)

This guide walks a brand-new curator from a clean machine to an
exported, validated translated ePub. It assumes you've already
followed the README's _Quickstart (development)_ section to install
`uv`. If you're running the released package instead, swap
`uv run epublate` for `epublate` (or `uvx epublate ...`) throughout.

The example uses the in-tree [`docs/Sample.epub`](Sample.epub) so you
can take the whole loop for a spin without an LLM key.

---

## 1. Launch the TUI

The default home is the TUI. Run `epublate` with no arguments and you
land on the **Projects screen**: a list of recently-opened projects
plus the keys that drive the rest of the app.

```bash
uv run epublate --mock-llm
```

![Projects landing screen](screenshots/01-projects.png)

| Key       | Action                                                                |
| --------- | --------------------------------------------------------------------- |
| `n`       | New project (modal: source ePub, target / source lang, out dir)       |
| `o`       | Open an existing project by path                                      |
| `enter`   | Open the highlighted recent project                                   |
| `delete`  | Drop the highlighted entry from recents — confirm `y/n`; files kept   |
| `D`       | **Delete project** — wipe folder + SQLite DB; confirm by typing name  |
| `r`       | Refresh / prune entries whose folders no longer exist                 |
| `T`       | Cycle theme (dark → light → high-contrast)                            |
| `? / F1`  | Cheat sheet for the current screen                                    |
| `q`       | Quit                                                                  |

`delete` is reversible — it only forgets the entry in
`~/.config/epublate/recents.json`. `D` (capital) is the destructive
twin: it wipes the whole project folder (the SQLite DB, the
`original.epub` copy, every export) after the curator types the
project name back as a hard confirmation. Use it for abandoned
experiments; reach for `delete` for anything you might still want.

The recents list lives at `~/.config/epublate/recents.json` and is
written every time you create or open a project (whether from the
TUI or the CLI). Pressing `enter` on a row pushes the Project
Dashboard.

Pressing `n` opens the **New Project** modal (or `o` for **Open
Project**); both let you bootstrap or import a project without
leaving the TUI:

| New project (`n`) | Open project (`o`) |
| --- | --- |
| ![New project modal](screenshots/02-new-project.png) | ![Open project modal](screenshots/03-open-project.png) |

## 1b. CLI bootstrap (scripts / CI)

The TUI flow is the recommended path. If you want to scaffold a
project headlessly — for CI, automation, or muscle memory — every
modal has a CLI sibling:

```bash
rm -rf /tmp/epublate-sample
uv run epublate --mock-llm new docs/Sample.epub \
    --source-lang en --target-lang pt \
    --out /tmp/epublate-sample
```

`new` does three things in one transaction:

1. Copies the source ePub to `<out>/original.epub` (the canonical
   read-only artifact for the project).
2. Creates the per-project SQLite database (`<out>/<name>.epublate`)
   in WAL mode and runs Alembic migrations against it.
3. Imports chapters and segments into the DB so every following
   command is a pure read/update against that file.

You can pass `--intake` to also run the optional helper-LLM book
intake (PRD §7.1 / M5). Skip it when you want a fast bootstrap and
plan to translate interactively from the Reader.

## 2. Open the Dashboard

From the TUI, press `enter` on a recents row, or use:

```bash
uv run epublate --mock-llm open /tmp/epublate-sample
```

The Dashboard is the landing screen for an open project. It shows
progress, cost, the curator inbox digest, and recent activity.

![Project Dashboard](screenshots/04-dashboard.png)

Key bindings (PRD §4.6 / M6):

| Key       | Action                                                             |
| --------- | ------------------------------------------------------------------ |
| `o`       | Reader (interactive segment-by-segment translation)                |
| `g`       | Glossary curator                                                   |
| `i`       | Inbox (flagged segments, proposed entries, alerts)                 |
| `b`       | Run a batch translation                                            |
| `c`       | **Cancel batch** — let in-flight calls finish, stop submitting more |
| `x`       | **Save ePub** — write the translation to disk (works at any point) |
| `B`       | Set / clear the project budget cap                                 |
| `e`       | Helper-LLM book intake (M5)                                        |
| `L`       | LLM activity (deep cost auditing, recent calls)                    |
| `s`       | Settings (read-only LLM config, theme, budget)                     |
| `r`       | Refresh                                                            |
| `q` / `esc` | Back to the previous screen                                      |
| `T`       | Cycle theme (`textual-dark` → `textual-light` → `epublate-contrast`) |
| `?` / `F1`| Open the cheat sheet for the current screen                        |

The batch worker lives on the App, not on the Dashboard. That means
you can dispatch a batch with `b`, leave the project (back out to
the Projects screen, even open a different project) and the run
keeps progressing in the background until it finishes, hits the
budget cap, or you press `c` to cancel. While a batch is active the
Dashboard and Reader both render a live progress strip showing the
chapter count, segment count, state badge (running / cancelling /
paused / cancelled / done), and a two-token cost line — `this batch
$X · project total $Y` — so you can tell at a glance how much the
running batch added on top of the project's pre-batch spend.

A slim **persistent batch status bar** docks at the bottom of every
other main screen (Glossary, Inbox, Settings, LLM activity, and the
Projects landing page) so wandering off the Dashboard never loses
the running tally. The bar disappears the moment the worker drains
and re-appears the moment a new batch starts, so an empty bar always
means "no batch in flight". It mirrors the same state badge and
counters as the Dashboard panel, just compressed onto a single line.

`q` and `Escape` both back out of the current screen everywhere except
the Projects landing screen, where `q` quits. The Escape variant is
hidden from the binding bar to keep the footer compact, but it works
identically.

The cheat sheet introspects the active screen's `BINDINGS`, so it
stays accurate when new actions land. Press `?` (or `F1`) anywhere to
overlay it on the current screen:

![Help / cheat sheet overlay](screenshots/09-help.png)

The chosen theme persists across runs in
`~/.config/epublate/ui.toml` (the file is created on demand and
otherwise managed by the TUI; you don't need to edit it by hand).
The `T` keybinding rotates through the four bundled themes —
`epublate` (the warm default), `textual-dark`, `textual-light`, and
the WCAG-AA-tuned `epublate-contrast`:

| `epublate` (default) | `textual-dark` |
| --- | --- |
| ![epublate theme](screenshots/04-dashboard.png) | ![textual-dark](screenshots/10-dashboard-theme-textual-dark.png) |

| `textual-light` | `epublate-contrast` |
| --- | --- |
| ![textual-light](screenshots/11-dashboard-theme-textual-light.png) | ![epublate-contrast](screenshots/12-dashboard-theme-contrast.png) |

## 3. Translate

Two paths are available:

* **Interactively** — press `o` from the Dashboard to open the Reader.
  `t` translates, `j`/`k` navigate segments, `J`/`K` navigate
  chapters, `a` accepts, `e` edits, `r` retries.

  ![Reader screen](screenshots/05-reader.png)

* **Headlessly** — exit the TUI and run a batch:

  ```bash
  uv run epublate --mock-llm batch /tmp/epublate-sample \
      --concurrency 2 --budget 1.00
  ```

  Failures (placeholder-mismatch, locked-glossary violations, LLM
  errors) land in the Inbox; the run pauses if the budget cap is hit.

## 4. Curate

The Inbox (`i` from the Dashboard) groups three kinds of work:

* **Flagged segments** — re-translate or accept-as-is.
* **Proposed glossary entries** — promote to `confirmed` / `locked`,
  edit the target term, or reject. Promoting a `locked` entry can
  trigger a cascade re-translation (PRD F-G-7).
* **Alerts** — budget pauses, LLM errors, intake summaries.

![Curator Inbox](screenshots/07-inbox.png)

Use `g` for the full Glossary curator: edit translations, lock
entries, manage aliases, view per-entry revision history. The right
pane shows the highlighted entry's notes, alias list, mention count,
and revision log:

![Glossary curator](screenshots/06-glossary.png)

## 5. Inspect cost / progress

```bash
uv run epublate stats /tmp/epublate-sample --json
uv run epublate budget show /tmp/epublate-sample
uv run epublate inbox /tmp/epublate-sample
```

The Dashboard shows the same numbers live; the CLI versions are
useful for shell scripts and CI dashboards.

For deeper LLM cost auditing — per-model spend, per-purpose
breakdown (translate vs extract vs tone-sniff vs cascade), and a
recent-calls table with timestamps and segment ids — open the
**LLM activity** screen with **`L`** from the Dashboard. The
Dashboard's compact panel surfaces the latest few calls inline; the
full screen lifts the limit so you can see where the budget is
going across an entire run. Both views also report input/output
token counts (read from the API's ``usage`` block when present, or
counted with ``tiktoken`` as a fallback) so a runaway prompt or
output is obvious.

## 6. Export the translated ePub

You can save the project as an ePub at **any point** — partial
translations work too. Untranslated segments fall back to the source
text per PRD F-IO-7, so the file is always valid and re-readable in
any reader.

### From the TUI (recommended)

Press **`x`** ("Save ePub") on the Project Dashboard. The modal that
opens lets you tweak the output path — by default it lands next to
`original.epub` as `<book-stem>.<target-lang>.epub` so successive
runs at different translation stages don't overwrite each other.
Click **Browse…** (or press `Ctrl+O`) to pick a different folder; the
filename stays editable. Submit with **`Ctrl+S`**.

The export runs in a background worker, so you can keep batch
translation running and still hit `x` to grab the latest snapshot.
The status bar reports the result; the file is ready as soon as
"Saved …" appears.

### From the CLI

The simplest export writes a (possibly partial) ePub atomically.

```bash
uv run epublate export /tmp/epublate-sample \
    --out /tmp/epublate-sample.epub
```

### Validate with epubcheck (M6)

Two new flags wire the optional epubcheck integration (PRD F-IO-6):

```bash
# Warn-only: print a one-line summary, never block.
uv run epublate export /tmp/epublate-sample \
    --out /tmp/epublate-sample.epub --epubcheck

# Strict: exit non-zero (3) on any error; print up to 5 messages.
uv run epublate export /tmp/epublate-sample \
    --out /tmp/epublate-sample.epub --strict
```

Validation requires the `[epubcheck]` extra (which bundles the
upstream Java JAR) and a working JRE:

```bash
# In a development checkout
uv sync --extra epubcheck

# Or for an installed CLI
uv tool install "epublate[epubcheck]"
```

If the extra isn't installed, or Java isn't on `PATH`, `--strict`
still succeeds — it just prints `epubcheck skipped: ...`. That keeps
the installation footprint small for curators who don't need
external validation.

## 7. Configure a real LLM endpoint

Drop `--mock-llm` and export the canonical environment variables:

```bash
export EPUBLATE_LLM_BASE_URL=https://api.openai.com/v1
export EPUBLATE_LLM_API_KEY=sk-...
export EPUBLATE_LLM_MODEL=gpt-5-mini
export EPUBLATE_LLM_HELPER_MODEL=gpt-5-mini  # optional, defaults to $EPUBLATE_LLM_MODEL
```

Any OpenAI-compatible endpoint works (Azure OpenAI, OpenRouter,
Together, Ollama, vLLM, llama.cpp). The Settings screen (`s` on the
Dashboard) shows the resolved values with the API key redacted to
the first four / last two characters:

![Settings screen](screenshots/08-settings.png)

## Where things live on disk

```
/tmp/epublate-sample/
  original.epub        # canonical source, never written after `new`
  <name>.epublate      # the SQLite DB (everything: segments, glossary,
                       #   LLM calls, events, edits, decisions)
  <name>.epublate-shm  # SQLite shared-memory file (WAL mode)
  <name>.epublate-wal  # SQLite write-ahead log
~/.config/epublate/
  ui.toml              # persisted theme choice (and other UI prefs)
```

Everything project-scoped lives in the project dir; the user-level
`ui.toml` only carries machine-wide preferences. There is no other
state — quitting and reopening the TUI is always safe.
