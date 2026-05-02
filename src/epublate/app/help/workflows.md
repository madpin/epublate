# Workflows

Step-by-step recipes for the tasks you'll actually do.

## Translate a new book end-to-end

1. **Projects screen** → press `n` for *New project*.
2. Pick a source ePub, a source language, and a target language.
3. Optionally tweak the tone preset (Settings → Style guide).
4. **Dashboard** → press `e` to run *Intake* over the first ~30
   segments. Helper LLM proposes proper-noun entries.
5. **Glossary** (`g`) → triage the proposed entries. Press `l` on
   anything you want the translator to honor exactly.
6. **Dashboard** → press `b` for *Batch*. Pick a chapter range,
   commit. Watch the progress meter and cost climb.
7. **Reader** (`o`) → spot-check translated segments side-by-side
   with the source.
8. **Inbox** (`i`) → resolve flagged segments (locked-glossary
   violations, validator complaints).
9. **Dashboard** → press `x` to *Save ePub*. The file goes wherever
   you point it; partial translations fall back to source text so the
   ePub is always valid.

## Resume a half-done book

1. **Projects screen** → press `o` for *Open*; pick the project
   folder.
2. **Dashboard** shows you exactly where you left off: progress
   percent, last batch event, pending segments. The DB is in WAL
   mode and every state-changing action is committed in a single
   transaction, so an OS crash mid-batch is safe.
3. Press `b` to keep going. Cached calls (same model + prompt +
   glossary state) are free — only the new segments cost money.

## Build a Lore Book from a translated edition (Phase 3)

This is the canonical use case for *target-only* lore: you have books
1-3 of a series already translated by hand, and you want to translate
book 4 without breaking the proper-noun consistency.

1. **Lore Books** tab → press `n` for *New Lore Book*; name it after
   the series (e.g. "Witcher Lore PT").
2. **Lore Book Dashboard** → press `I` for *Ingest target*.
   Pick the already-translated `book1-pt.epub`. The helper LLM
   extracts proposals as canonical *target* terms (no source term
   yet).
3. **Glossary** screen of the Lore Book → review proposed entries,
   lock the canonical names you care about.
4. Open the translation **Project** for book 4 → **Settings → Lore
   Books** → *Attach Witcher Lore* (mode: writable).
5. Translate book 4 as normal (`b`). The translator sees the
   canonical target names in its prompt and uses them; new
   source-side proposals it discovers accumulate in the attached
   Lore Book, building the bilingual map across the whole series.

## Run epubcheck on the export

Pass `--epubcheck` to the CLI (`epublate export <project> --epubcheck
--out final.epub`) or tick the *Run epubcheck* box in the Export
modal. The report is stored as `project.epubcheck_completed` in the
event log.

## Review the audit log

Every state-changing action records an event. Find them in the
Dashboard's *Recent activity* panel (last 5) or by querying the
project DB directly:

```
sqlite3 path/to/project/<stem>.epublate \
  'select kind, payload_json from event order by id desc limit 20;'
```
