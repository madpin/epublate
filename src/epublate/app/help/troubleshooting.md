# Troubleshooting

## "Validator failed" on a batch run

Open the **Inbox** (`i`); flagged segments live there with the
specific reason (placeholder mismatch vs. locked-glossary violation).

- **Placeholder mismatch** (`[[T0]]` count differs between source and
  target): the model dropped or duplicated an inline tag. Re-translate
  the segment from the Reader (`o` then `t`); the prompt re-includes
  the placeholders for re-extraction.
- **Locked-glossary violation**: the source carried a locked term but
  the target didn't use the canonical translation. Either fix the
  segment manually, soften the entry to `confirmed` (warning, not
  error), or update the canonical target term and **cascade**.

## "Budget cap reached"

The Dashboard's status line shows `batch.paused`. Either:

- Raise the cap (Settings → Project → Budget USD or `B` on Dashboard).
- Clear the cap entirely (set the field empty, save).
- Wait until the next session.

A paused batch is *not* lost — pressing `b` again resumes from the
first untouched segment.

## "Cache mismatch" / unexpected re-translations

The cache key includes:

- the model name,
- the system prompt hash (style guide → translator prompt),
- the user prompt hash (segment text + placeholders),
- the glossary state hash.

Any change to those will *correctly* invalidate the cache; you'll see
new LLM calls instead of cache hits in the LLM activity panel. To
debug a specific re-call, look at the `llm_call` table:

```
sqlite3 path/to/project/<stem>.epublate \
  'select model, purpose, cache_hit, cost_usd from llm_call \
   order by created_at desc limit 10;'
```

## Cover image isn't showing

The Dashboard shows the cover slot's filename + size, not the
rendered image (we don't ship a TUI image dependency in v1). The
cover *will* be embedded correctly in the exported ePub regardless.

## Mock LLM didn't kick in

Either pass `--mock-llm` to the CLI or set `EPUBLATE_LLM=mock` in
the environment. The Settings → LLM panel banner shows you the
currently effective provider and model.

## "no helper model" error during intake

Settings → LLM has a *Helper model* override; if that's blank we
fall back to `EPUBLATE_LLM_HELPER_MODEL` and finally to the
translator model. Pick one of those three.

## SQLite "database is locked"

You probably have two epublate processes pointed at the same project
folder. The DB is opened in WAL mode (concurrent reads OK) but
*writes* serialize. Close the duplicate process or wait for it to
release the lock.
