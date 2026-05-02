# Concepts

These are the core ideas that show up across every screen. The
**bold** word is the term you'll see in menus, status lines, and the
events log.

## Project

A **project** is one folder on your disk: an immutable copy of the
source ePub plus a per-project SQLite database
(`<book-stem>.epublate`). Everything you do — translations, glossary
edits, audit events — lives in that single file. Move the folder
anywhere; the project moves with it. There is **no global state**
beyond `~/.config/epublate/ui.toml` (theme + global preferences).

Open / close a project from the **Projects** screen
(`epublate` with no arguments).

## Lore Bible (per-project glossary)

The **lore bible** is the project's glossary of canonical translations
for proper nouns, places, organizations, etc. Three statuses:

- `proposed` — suggested (by you, by intake, or by a translator
  cascade); doesn't influence translation yet.
- `confirmed` — recommended; the translator sees them as preferences,
  the validator warns when they are violated.
- `locked` — non-negotiable. The validator **hard-fails** any segment
  whose source contains a locked source-term but whose target doesn't
  use the canonical target-term (PRD invariant 1.2).

You can **lock** an entry from the Glossary screen (`g` then `l`) or
the Inbox (`i`).

## Intake

**Intake** = "scan source text for proper nouns and seed the lore
bible." It runs the helper LLM over the first N segments and proposes
entries for the curator to triage. **No translation happens during
intake** — it's purely a discovery pass.

You can run intake at any point (`e` on the Dashboard) or have it run
automatically when you create a new project (Settings → Intake →
"Run after new project").

## Batch

**Batch** = translate many segments in one run. The Batch modal
(`b` on the Dashboard) lets you scope by chapter range, set
concurrency / retries, and override the model on the fly. The run
streams progress messages back to the Dashboard so you can watch the
counts climb without leaving the screen.

A batch will **pause** when it hits the project's budget cap
(see Settings → Project → Budget USD); resume by raising the cap or
pressing batch again.

## Cascade

When you change a confirmed/locked glossary entry, **cascade** marks
every previously translated segment that mentions the old term as
"needs re-translation." Triggered from the Glossary screen
(`g` then highlight an entry, then `c`).

## Validator

The **validator** is the post-translate guardrail. It checks:

1. Every inline-tag placeholder (`[[T0]]…`) the translator returned
   matches one we put in the prompt (PRD invariant 1.1).
2. Every locked glossary term is honored (PRD invariant 1.2 —
   exception: target-only Lore Book entries are *soft-locked*; see
   the Workflows tab).

A validator failure marks the segment `flagged` and surfaces it in
the Inbox (`i`).
