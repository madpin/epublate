# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — Logs screen, app-wide toasts, glossary cleanup, batch-progress polish

Four user-reported pain points addressed in one pass:

1. **Batch progress disappeared on navigation.** Backing out of a
   running project to the Projects screen and reopening the same
   project showed an empty Dashboard rich progress panel — the
   freshly-mounted `DashboardScreen` reset its `_batch_running`
   flag to ``False`` and never re-attached as a batch listener.
   Other screens (Inbox, LLM activity) had a `BatchStatusBar` but
   it was washed-out (`$accent 20%` background) and curators kept
   missing it during long batches.
2. **Inconsistent submit semantics.** Some form modals accepted
   ``Enter`` to submit (NewProject, OpenProject), others required
   ``Ctrl+S`` (Batch, Budget, Intake, EntryEdit, LoreIngest, …);
   the curator had to remember which was which.
3. **Glossary entries too large or near-duplicate.** The helper
   LLM occasionally proposed full sentences as "phrases"
   (`First you will eat your chickens, then your goats, …`),
   broken-paren artefacts (`Fédération … (FIFA` with no close
   paren), and near-duplicates (`HIPC` vs
   `HIPC initiative`) that the auto-proposer's exact-source-term
   dedup let through.
4. **Logs / errors invisible in the TUI.** Batch failures, rate
   limits, and pipeline errors landed in `_logger.warning` /
   `_logger.exception` only — the curator had no in-TUI way to see
   "what just went wrong?".

#### Workstream A — Batch progress visibility

- `epublate.app.screens.dashboard.DashboardScreen.on_mount` →
  re-attach to the App-level batch when one is active for this
  project (re-paints `BatchProgressMeter` from
  `EpublateApp.batch_progress.summary` and re-registers the
  listener).
- `epublate.app.screens.dashboard.DashboardScreen.on_unmount` →
  release the App-level batch listener slot when it was ours.
- `epublate.app.widgets.batch_status_bar.BatchStatusBar` → solid
  background for `-active` / `-cancelling` / `-paused` (was 20%
  wash); high-contrast `▶ BATCH` ribbon prefix; updated docstring
  reflecting the "every screen" mounting policy.
- `BatchStatusBar` now mounts on Dashboard (in addition to its
  rich panel — the slim bar surfaces *cross-project* App-level
  state), Reader, Lore Books, and Lore Book Dashboard. It already
  lived on Inbox / Glossary / LLM activity / Settings.

#### Workstream B — Modal submit consistency (`Enter` *and* `Ctrl+S`)

Every form modal (`BatchModal`, `BudgetModal`, `IntakeModal`,
`EntryEditScreen`, `LoreIngestModal`, `LoreImportProjectModal`,
`NewLoreBookModal`, `OpenLoreBookModal`) now overrides
`on_input_submitted` to call its `action_submit` /
`action_save` so ``Enter`` from any single-line `Input` is
equivalent to ``Ctrl+S``. Footer hint strings unified to
`Press Enter or Ctrl+S to <verb>, Esc to cancel.`. The Reader's
`EditTargetScreen` is the documented exception (its `TextArea`
keeps ``Enter`` as content; only ``Ctrl+S`` saves).

New rule `.cursor/rules/tui-keybindings.mdc` codifies the
convention so future modals don't drift back into "only `Ctrl+S`
works".

#### Workstream C — Glossary quality (prevention + cleanup)

- `epublate.llm.prompts.extractor` → tightened the system prompt
  with explicit length caps ("noun phrase, named entity, or short
  fixed expression — at most 10 words and 100 characters") and
  the `Full Name (ACRONYM)` canonicalisation rule. New parser
  caps (`EXTRACTOR_MAX_WORDS`, `EXTRACTOR_MAX_CHARS`,
  `_violates_extractor_caps`) drop full-sentence proposals,
  unbalanced-paren artefacts, and runaway-length candidates at
  the parser boundary. Logged at DEBUG so noisy endpoints can be
  spotted without spamming INFO.
- `epublate.llm.prompts.extractor_target` → same caps applied to
  the target-language extractor used by Lore Book ingest, with
  the alias list filtered through the same predicate.
- `epublate.glossary.dedup` → new module exposing
  `canonical_form` (NFKC + lowercase + paren acronym strip + a
  conservative trailing common-noun strip — repeated to a fixed
  point so `Foo (BAR) initiative` and `Foo` collapse to the same
  bucket) and `find_near_duplicates` (canonical-form bucketing
  plus a Levenshtein guard for residual fuzzy matches).
- `epublate.glossary.io.upsert_proposed` → after the exact
  source-term lookup misses, scan the project glossary by
  canonical form. When a match is found, fold the variant
  spelling in as a `source_alias` instead of creating a second
  proposed entry. Catches the user-reported HIPC / FIFA shapes
  before they land in the DB.
- `epublate.app.screens.glossary.GlossaryScreen.action_merge_duplicates`
  → unified the existing `m` "Merge dupes" action under the new
  `find_near_duplicates`. Curator confirms each group via the
  existing `MergeDuplicatesScreen` (per
  `glossary-invariants.mdc` §4 "no silent merges"). Binding
  description updated to "Cleanup dupes" to reflect the broader
  scope.

#### Workstream D — Logs screen + toast notifications

- `epublate.app.log_buffer` → new in-memory `RingBufferHandler`
  (default 2000 records on the package-root `epublate` logger).
  Pure observer; clears on app restart by design (NFR-7 / "no
  telemetry" — Python logs stay ephemeral, the persistent audit
  trail is still the `event` table).
- `epublate.app.screens.logs.LogsScreen` → new screen merging
  three streams newest-first: project events, the ring buffer's
  log records, and `llm_call` rows. Source filter (`f`) cycles
  `events+logs` (default) → `all` → `events` → `logs` → `llm`;
  level filter (`l`) cycles `all` → `warning+` → `error+` for
  the log stream; time filter (`t`) cycles `24h` (default) →
  `today` → `all`; `/` opens a substring search; row highlight
  expands a detail pane below the table with the full payload
  (event JSON, log traceback, `llm_call` request/response).
  Bound to lowercase ``l`` from the Dashboard (uppercase ``L``
  still opens the LLM-activity deep-stats screen). Resolves the
  PRD §4.6 "Logs" entry that was specced in M0 but never built.
- `epublate.app.main.EpublateApp._notify_batch_done` → toasts
  routed through `app.notify` for `failed` / `cancelled` /
  `paused` and `completed-with-flags`. Lands on whatever screen
  is focused, so an error that happened "while you were looking
  somewhere else" is still visible.
- `epublate.app.screens.reader._handle_pipeline_failed` and
  `_handle_chapter_batch_failed` toast via a new `_notify_safe`
  helper that swallows the rare race between the worker and an
  unmounted screen.

#### Tests

- `tests/test_glossary_dedup.py` — canonical-form known cases,
  HIPC/FIFA grouping, Levenshtein-guard typo case, target-only
  dedup, deterministic group ordering.
- `tests/test_extractor_caps.py` — eat-your-chickens sentence
  rejected, `Heavily Indebted Poor Country (HIPC)` accepted,
  `(FIFA` with broken paren rejected, target-side aliases
  filtered, `Sammy Davis Jr.` accepted (single trailing period
  is allowed).
- `tests/test_app_log_buffer.py` — capacity, wraparound,
  snapshot-copy semantics, args-mismatch resilience,
  install/uninstall round-trip, level / substring / logger-prefix
  filters.
- `tests/test_app_dashboard_batch_reattach.py` — re-mount with a
  running batch reattaches the listener and shows the rich
  panel; cross-project batches don't leak into the wrong
  Dashboard; `on_unmount` releases the listener slot.
- `tests/test_app_modal_enter_submit.py` — every form modal
  covered (`BatchModal`, `BudgetModal`, `IntakeModal`,
  `EntryEditScreen`, `LoreIngestModal`, `NewLoreBookModal`)
  accepts ``Enter`` to submit.
- `tests/test_app_logs_screen.py` — events + log records render,
  source filter cycle adds LLM rows, summary helper extracts
  known event payloads.

#### Docs / rules

- `.cursor/rules/tui-keybindings.mdc` — new rule codifying
  `Enter`/`Ctrl+S`/`Esc` semantics on modals, `r` = Refresh on
  view screens, `r` = Retry as the documented Reader exception.
- `docs/PRD.md` — Logs screen marked "shipped" alongside its
  M0/M4 sibling entries; entry added to M6 polish list.

### Fixed — Rate-limit (HTTP 429) pauses the batch instead of failing every segment

A curator running a batch against the OpenRouter free tier hit
`Rate limit exceeded: free-models-per-day. Add 6.55 credits to unlock
1000 free model requests per day` *per segment* for the rest of the
run — the daily quota was depleted, every retry burned a worker, and
the meter looked like translation was happening when nothing was
landing in the database. Two compounding causes:

1. The OpenAI-compatible provider treated 429 as a generic retryable
   status (4 quick retries with 0.5–8s backoff) and then surfaced it
   as ``LLMResponseError``. A free-tier daily quota resets at UTC
   midnight, *not* in 8 seconds — every retry was wasted.
2. The batch worker caught the resulting error as an opaque per-segment
   failure, recorded it in ``summary.failures``, and immediately moved
   on to the next segment, which hit the same 429.

Fix: the provider now distinguishes *short* rate-limit waits (server
hint ``Retry-After: 5`` → honor it, retry within budget) from *long*
waits (``X-RateLimit-Reset`` minutes-to-hours away → lift out as a
typed ``LLMRateLimitError``). The batch runner catches that error
class in both worker boundaries — translator and helper-LLM
pre-pass — and pauses the run with the provider's verbatim message in
the pause reason (so the curator sees "Add 6.55 credits to unlock
1000 free model requests per day" or "resets in ~5h" without digging
through logs). Pending segments stay ``pending`` so the run resumes
cleanly after the curator switches model, adds credits, or waits.

- ``epublate.errors.LLMRateLimitError`` → new typed exception. Subclass
  of ``LLMResponseError`` (so callers that already catch the parent
  see it) carrying ``retry_after_seconds`` and ``provider_message``
  for actionable surfacing.
- ``epublate.llm.openai_compat`` → 429 is no longer in the generic
  retryable-statuses set. ``RateLimitError`` is caught explicitly:
  ``Retry-After`` ≤ 120s sleeps and retries within the per-call
  budget; ``X-RateLimit-Reset`` more than 120s away (free-tier daily
  quotas, monthly caps) raises immediately.
- ``epublate.llm.json_mode.chat_with_json_fallback`` → never silently
  retries a 429 as a "JSON-mode rejected" fallback, even though
  ``LLMRateLimitError`` is technically a subclass of
  ``LLMResponseError``.
- ``epublate.core.batch.run_batch`` → catches ``LLMRateLimitError``
  from both translator workers (per-future) and helper pre-pass
  (per-chapter), pauses the run with a curator-friendly reason
  (``rate limit hit: <provider message> (resets in ~Xh; switch model,
  add credits, or wait and resume)``).
- ``epublate.core.extractor.run_pre_pass`` → re-raises
  ``LLMRateLimitError`` so the rate-limit signal isn't lost in the
  per-chunk failure breaker, and emits a new
  ``batch.pre_pass_rate_limited`` audit event (rendered in Inbox /
  Dashboard with the provider message and reset hint).
- ``epublate.app.screens.reader._handle_chapter_batch_finished`` →
  surfaces ``summary.paused_reason`` verbatim in the status bar
  (curator sees the actionable hint without opening Inbox) and clears
  the chapter queue on a pause so the rest of the queue doesn't
  silently fail-and-pause-again on the same wall.

Tests cover (a) the depleted-quota path
(``X-RateLimit-Reset`` an hour away → ``LLMRateLimitError`` on the
first call, no per-call retries burned), (b) the canonical short
``Retry-After`` honored within the existing retry budget, (c) the
header-less 429 fallback (legacy retry path preserved), (d) retries
exhausted surfacing as ``LLMRateLimitError`` (not generic
``LLMResponseError``), (e) translator rate-limit pauses the batch
without recording a per-segment failure, (f) helper pre-pass
rate-limit pauses the run before any segment translates.

### Fixed — Batch pre-pass no longer blocks translation start

A curator running the M5 batch pre-pass against a slow helper model
(any reasoning model on a permissive endpoint, e.g. OpenRouter
Nemotron at ~25s/chunk) reported that "the batch from the dashboard
does a few helper-model calls but never starts to translate the
segments". Diagnosis: ``run_batch`` ran *every* chapter's pre-pass
before submitting *any* segment to the translator pool. For a
30-chapter book, that's 30 × 30s ≈ 15 minutes of helper calls before
the segment-count meter ticks once — long enough to look like a
deadlock. ``cancel_event`` was only checked inside the translator
dispatch loop, so Cancel was a no-op during pre-pass; the budget cap
also wasn't enforced between chapters, so a runaway helper could
blow past the cap before the cap-check fired.

Fix: pre-pass and translation now **interleave per chapter**.
``run_batch`` walks chapters in spine order and, for each chapter,
runs pre-pass → submits that chapter's segments to the (shared)
thread pool → drains them → moves on. First translation event lands
within ~30s on a fresh run instead of after every chapter's pre-pass
completes. Trade-off (intentional): chapter N's translator only sees
``proposed`` glossary entries discovered through chapters 1..N — but
book-wide consistency comes from the Lore Book / Book Intake (M5,
F-LB-1..F-LB-9), not from the per-chapter pre-pass.

- ``epublate.core.extractor.run_pre_pass`` → new optional
  ``cancel_event: threading.Event | None`` (checked between chunks,
  emits ``batch.pre_pass_cancelled`` on early exit) and ``on_chunk:
  Callable[[PrePassChunkEvent], None] | None`` (fires once per chunk
  so the orchestrator can surface "pre-pass: ch X/Y chunk A/B").
- ``epublate.core.batch.run_batch`` → new optional
  ``on_pre_pass_progress: Callable[[BatchPrePassProgress], None]``
  callback. Pre-pass spend is folded into ``summary.cost_usd``
  *between* chapters so the budget cap is enforced inside the helper
  loop, not after it (PRD F-LLM-7 / F-LLM-8). Cancel is checked
  before each chapter's pre-pass and inside ``run_pre_pass``'s chunk
  loop, so curators can stop a slow pre-pass at any time.
- ``epublate.app.main.EpublateApp._post_pre_pass_progress`` →
  forwards ticks to the Dashboard listener as a new
  ``BatchPrePassTick`` message; the Dashboard renders
  "pre-pass: ch X/Y chunk A/B {ok|cached|failed (...)}" in the
  status footer so a slow helper looks like progress, not a hang.
- ``epublate.core.stats.ALERT_KINDS`` picks up
  ``batch.pre_pass_cancelled`` and ``batch.pre_pass_aborted`` so
  both terminations show up in the recent-activity strip with
  context (``cancelled after N chunks``, ``aborted after K
  failures``).

This is a behavior change for the helper extractor (the per-chapter
pre-pass run happens later than before — at most one chapter's
worth of helper output is "ahead of" the translator at any moment)
but doesn't move any product invariant: the translator still sees
every locked / confirmed / proposed entry that exists at the moment
its segment is submitted, and the existing
``test_run_batch_pre_pass_seeds_glossary_before_translating`` still
passes (chapter 1's pre-pass still runs before chapter 1's
translation). New tests pin down the new semantics:

- ``test_run_batch_pre_pass_interleaves_with_translation_per_chapter``
  records LLM call order and asserts a translator call lands before
  the second chapter's extractor call.
- ``test_run_batch_pre_pass_cancels_promptly`` sets cancel inside the
  first helper response and asserts ``BatchCancelled`` lifts out
  *without* any chapter's translator running.
- ``test_run_batch_pre_pass_budget_cap_pauses_before_translation``
  forces a non-zero helper price, sets a tiny cap, and asserts
  ``BatchPaused`` lifts before any segment translates.
- ``test_run_batch_pre_pass_emits_progress_callback`` asserts
  ``on_pre_pass_progress`` fires for every chunk in every chapter,
  in chapter order, with valid 1-based indices.

### Added — `EPUBLATE_LLM_REASONING_EFFORT` knob for reasoning models

Curators on reasoning models (``gpt-5-*``, ``gpt-oss-*``, ``o1-*``,
``o3-*``, OpenRouter's Nemotron ``-reasoning`` slugs) reported the
pipeline being "very very slow" — the user-facing translation step
sat at ~30s per segment because every call burned several thousand
hidden chain-of-thought tokens before emitting the JSON the
pipeline cares about.

Fix: ``OpenAICompatProvider`` now exposes the standard OpenAI
``reasoning_effort`` field (``minimal | low | medium | high``).
Lowering it cuts latency dramatically — the smoke probe against
``nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`` dropped from
30.2s → 13.6s for one translator segment with ``low`` (55% faster),
with no quality regression on the 1-segment translator and
pre-pass-size extractor probes.

- ``OpenAICompatProvider.reasoning_effort`` is the new field;
  defaults to ``None`` so non-reasoning models on stricter endpoints
  (which would reject the unknown parameter) keep working.
- Validation is at construction time (``LLMResponseError`` for an
  invalid value) so a typo in ``$EPUBLATE_LLM_REASONING_EFFORT``
  fails fast instead of surfacing as a 400 mid-batch.
- ``epublate.llm.factory.build_provider`` reads
  ``$EPUBLATE_LLM_REASONING_EFFORT`` and the optional per-project
  ``reasoning_effort`` override (Settings → LLM panel will surface
  this in M6); the project override wins over the env default so
  one project can run on ``high`` (quality matters) while another
  runs on ``low`` (speed matters) using the same shell environment.
- Cache key (PRD §6.4) is *not* hashed by ``reasoning_effort``: a
  cached high-effort response served to a low-effort re-run is
  strictly an improvement (better answer, free), and the failure
  mode (low → high cache hit) is also fine because the cached
  answer was at least as good.

This is the standard OpenAI field, not provider-specific code, so
it doesn't violate AGENTS.md §2.5 ("OpenAI-compatible LLMs only").
OpenRouter forwards the field to upstream; non-reasoning models on
permissive endpoints just ignore it.

### Added — `scripts/smoke_llm_credentials.py` dev utility

A read-only probe that hits an OpenAI-compatible endpoint with the
*actual* prompt shapes the pipeline ships in production —
translator, group translator, extractor (1 paragraph + pre-pass
chunk size), and target-language extractor — through
:func:`epublate.llm.json_mode.chat_with_json_fallback`, then verifies
JSON parse and (for the translator) placeholder round-trip.

Configs live in a gitignored ``.smoke_configs.json`` (or any path
pointed at by ``$EPUBLATE_SMOKE_CONFIGS``); credentials never enter
git history. Per-config output names which channel is broken, the
specific failure mode (HTTP error / parse failure / empty visible
content / placeholder mismatch), and the per-call latency + token
spend. Useful when picking a helper model for a new endpoint or
when triaging "did the LLM regress overnight?". Bootstrap contract
is preserved — the probe is intentionally not part of
``uv run pytest``.

### Fixed — Helper-LLM loops now stop hammering broken endpoints

Even with the JSON-mode soft-fallback, some helper endpoints emit
empty visible content for *every* call (the canonical example:
``gpt-oss-20b`` on Groq returning the empty string after the fallback
retries without ``response_format``, because reasoning tokens consume
the entire visible-channel budget). The pre-pass and intake loops
were "best-effort, never abort" by design — useful when one chunk is
flaky, terrible when the endpoint is fundamentally broken: the loop
walked every chunk in the chapter / book, logged the same warning
per chunk, and kept asking the curator's wallet for more nothing.

Fix: every helper-LLM chunk loop now has a configurable
**consecutive-failure circuit breaker**. Three failures in a row
(``DEFAULT_FAILURE_STREAK_LIMIT``) abort the loop and emit a
structured event so the curator's Inbox surfaces the abort with the
same shape as the success path:

- ``epublate.core.extractor.run_pre_pass`` → emits
  ``batch.pre_pass_aborted`` (instead of ``batch.pre_pass_completed``)
  with ``failure_streak`` and a truncated ``last_error`` in the
  payload.
- ``epublate.core.extractor.run_book_intake`` → emits
  ``intake.aborted`` (instead of ``intake.completed``).
- ``epublate.lore.ingest_target.ingest_target_epub`` → emits
  ``lore.target_ingest_aborted`` (instead of ``lore.target_ingested``).
- ``IntakeOptions.failure_streak_limit`` is the per-call knob; set to
  ``0`` to keep the legacy "best-effort, never abort" behavior.
- ``ingest_target_epub`` exposes the same knob via
  ``failure_streak_limit`` (default
  ``DEFAULT_TARGET_FAILURE_STREAK_LIMIT``).
- A successful chunk resets the streak so a transiently flaky
  endpoint (one rate-limited call mid-chapter) does not abort the
  rest of the run.
- The single warning emitted when the breaker trips names the
  most likely root cause and a one-step fix
  (``... it may be returning empty visible content because reasoning
  tokens consume the entire visible-channel budget. Try a
  non-reasoning helper via $EPUBLATE_LLM_HELPER_MODEL or the project
  override.``).

This bounds the wasted-call cost on broken endpoints to ``failure_streak_limit``
helper calls per chapter / book — and gives the curator a clear,
actionable next step instead of a Ctrl+C.

### Fixed — Soft-fallback when helper endpoints reject JSON mode

After the previous fix turned on ``response_format=json_object`` by
default for helper-LLM calls, curators on Groq (proxied via LiteLLM)
started seeing a different failure mode for the same root cause:

```
pre-pass chunk failed: OpenAI API status 400: …
litellm.BadRequestError: GroqException - {"error":{"message":
"Failed to validate JSON. Please adjust your prompt. …",
"type":"invalid_request_error","code":"json_validate_failed",
"failed_generation":""}}
```

What's happening: Groq runs its grammar validator *after* generation.
For a reasoning helper (``gpt-oss-20b``), the model spends the entire
visible-channel token budget on reasoning tokens, the visible content
is the empty string, the validator can't validate "nothing", and the
endpoint returns a 400 instead of empty content. Same upstream cause
as before — just a louder failure surface.

Fix: ``epublate.llm.json_mode.chat_with_json_fallback`` is a thin
wrapper around ``provider.chat`` that catches ``LLMResponseError``
whose message indicates an endpoint-level rejection of
``response_format`` (matched on substrings such as
``json_validate_failed``, ``response_format``, ``json mode``) and
retries the call once **without** ``response_format``. The
prompt-level "respond with a single JSON object" instruction is the
only constraint left; endpoints that work cleanly (OpenAI proper,
OpenRouter, vLLM, the non-Groq LiteLLM backends) never hit the
fallback.

Wired in at every helper-LLM entry point:

- ``epublate.core.extractor.extract_entities`` (and therefore
  ``run_book_intake`` and ``run_pre_pass``).
- ``epublate.core.style_sniff.sniff_tone`` (the New-Project tone
  helper).
- ``epublate.lore.ingest_target._extract_target_chunk`` (target-Lore-
  Book ingest).

Trade-off: on incompatible endpoints, the first call to a fresh cache
slot now costs one extra round-trip (the rejected call). The fallback
is logged once per call as a structured warning, with the upstream
error truncated so the Inbox stays readable. Network-layer errors
(``LLMTransportError``) and unrelated 400s still propagate unchanged
so the OpenAI-compat retry policy and the curator's audit trail keep
their existing semantics. Genuinely cleaner: switch the helper to a
non-reasoning model (``llama-3.1-8b-instant``, ``gpt-5-mini``,
``Qwen3-32B``, …) — but the soft-fallback means ``gpt-oss-20b`` no
longer breaks the pre-pass on Groq either.

### Fixed — Helper-LLM extractor now requests JSON mode by default

Curators running reasoning-style helper models (notably ``gpt-oss-20b``
through LiteLLM) were seeing the pre-pass log line repeat per chunk:

```
pre-pass chunk failed: extractor response was empty
pre-pass chunk failed: failed to recover JSON from extractor response: …
```

Root cause: nothing in the codebase passed ``response_format`` to the
provider, so reasoning helpers spent the visible-channel token budget on
reasoning tokens and returned empty content (and on the rare occasion
they did emit JSON, prose around it broke the recovery regex). The
batch's per-chunk error handler logged each failure and continued, so
no work was lost — but every pre-pass effectively produced zero
``proposed`` glossary entries on those endpoints.

Fix: every helper-LLM extractor entry point now defaults to
``response_format=ResponseFormat(type="json_object")`` (the OpenAI
chat-completions JSON-mode contract, which our prompts already satisfy
with their "respond with a single JSON object" instruction).

- ``epublate.llm.prompts.extractor.DEFAULT_RESPONSE_FORMAT`` is the new
  shared constant; the regular extractor and the target-language
  extractor both export their own copy so each prompt module stays
  self-contained.
- ``ExtractOptions.response_format`` defaults to that constant via
  ``field(default_factory=...)`` (frozen-dataclass-safe). Both
  ``run_book_intake`` (Dashboard intake) and ``run_pre_pass`` (batch
  pre-pass) inherit the default automatically.
- ``epublate.core.style_sniff.sniff_tone`` now passes the same
  response format on the New-Project tone-sniff helper call.
- ``epublate.lore.ingest_target.ingest_target_epub`` flips its
  ``response_format`` parameter default from ``None`` to the JSON-mode
  constant, so target-Lore-Book ingests behave the same way.

Curators on endpoints that genuinely don't speak ``response_format``
can still opt out per call (``ExtractOptions(response_format=
ResponseFormat(type="text"))``); the prompt-level "JSON only"
instruction still applies, just unconstrained. The cache key is
unaffected — it's hashed from the messages and the glossary state, not
from ``response_format`` — so previously cached extractor calls remain
cache hits.

### Changed — Helper pre-pass is now on by default for UI batch / chapter translate

The helper-LLM pre-pass (PRD §4.2 phase 3 / M5) is the cheap
extractor scan that walks each chapter's pending segments before the
translator fires, surfacing fresh proper-noun candidates as
`proposed` glossary entries so the translator's prompt is
glossary-aware on the first attempt. It used to be reachable only
via `epublate batch --extract` on the CLI; the UI couldn't trigger
it at all. Curators who lived in the TUI were silently missing the
glossary-growth pass on every translate.

- **`BatchModal` (`b` from the Dashboard)** grew a `Helper pre-pass
  [y/n]:` row that defaults to `y`. Submitting the form unchanged
  enables the pre-pass; typing `n` opts out for that run only.
- **Reader chapter translate (`b`, sidebar button, queue ladder)**
  now passes `pre_pass=True` on the `BatchOptions` it hands to
  `run_batch`. The Reader has no modal so there's no toggle — the
  reasoning is that a per-chapter translate from the Reader is the
  same shape as a Dashboard batch, and the curator should get the
  same glossary growth either way.
- **Helper-model resolution** is centralized in both call sites: we
  call `resolve_helper_model(translator_model,
  project_overrides=...)` so the project's `helper_model` setting
  (Settings → LLM panel) and `EPUBLATE_LLM_HELPER_MODEL` actually
  win over the translator-model fallback that `run_batch` would have
  used otherwise. Failures in resolution fall back to the translator
  model rather than blocking the batch.
- **CLI default unchanged.** `epublate batch` keeps `--no-extract`
  as its default to preserve scripted/CI workflows that rely on the
  old behavior. Pass `--extract` to opt in (or leave the UI to do it
  for you).

### Changed — Glossary entries must have symmetric leading articles / prepositions

A curator audit surfaced a subtle but explosive bug: the helper LLM
was happily proposing entries like `Europe → na Europa` (asymmetric:
the target carries the contracted preposition + article, the source
doesn't). The translator dutifully applied the entry on the next
"in Europe" mention and emitted `"na na Europa"` — the same
preposition twice. The mirror case `the USA → EUA` shows the same
shape on the source side. Both are now blocked end-to-end (PRD
F-LB-3 / glossary-invariants §1).

- **`epublate.glossary.normalize`** is the new home for per-language
  leading-particle detection. Ships curated sets for English,
  Portuguese, Spanish, French, Italian, and German covering the
  common articles, prepositions, and prep+def contractions
  (`na/no/da/do/del/au/aux/im/zum`, …). Public surface:
  `leading_particle`, `normalize_term`, `analyze_pair`, and
  `find_doubled_particles` for the validator hook.
- **Auto-proposer (lemma form).** `glossary.io.upsert_proposed` now
  takes optional `source_lang` / `target_lang` and strips a single
  leading particle from each side before insert. The pipeline and
  the extractor pass the project's languages through, so a noisy
  helper-LLM proposal of `the USA → os EUA` lands as `USA → EUA`
  and `Europe → na Europa` lands as `Europe → Europa`. Curators
  can still author symmetric pairs (`the USA → os EUA`) manually
  via the edit modal — the auto path defaults to lemma because
  it's the unambiguous winner for the noisy proposer.
- **Curator save (symmetry required).** `EntryEditScreen` now
  validates the source/target pair on save: either both sides
  carry a leading article/preposition, or neither does. The save
  is rejected with an inline error pointing at the mismatch
  (`target "na Europa" starts with "na" but the source "Europe"
  has no matching article/preposition.`). Curators can fix it
  either way — drop the particle from the target (lemma form) or
  add the missing one to the source.
- **Runtime soft-warn validator.** A new `find_target_doubled_particles`
  enforcer pass scans the LLM's target output for adjacent identical
  function words (`"na na Europa"`, `"the the Senate"`). Hits are
  recorded as `severity="warning"` / `kind="doubled_particle"`
  Violation rows. The pipeline's flag rule moves from
  `has_locked_violation` to a new `has_flagging_violation` that
  flips the segment to `flagged` for either locked errors *or*
  doubled-particle warnings, so the curator sees them in the Inbox.
  No retry budget is consumed — this is meant to be a soft signal,
  not a hard failure.
- **Translator prompt rule.** A new bullet in both single-segment
  and grouped translator templates explicitly instructs the model
  about the lemma-vs-symmetric shape and forbids `"na na X"` /
  `"the the X"` style runs. The cache key folds the prompt content
  so this change invalidates any pre-existing translations that
  baked the older rule.

The user agreed to wipe existing project DBs rather than running a
migration over confirmed/locked entries, so no `epublate migrate`
helper ships in this release.

### Fixed — Auto-proposed glossary entries record their birthing segment as the first occurrence

The Glossary "Uses" column read `0` and the new Occurrences modal
sat empty for entries that were *just* auto-proposed, even though
the segment that birthed them obviously contained the term. Cause:
`translate_segment` ran `find_mentions` against the project glossary
*before* `_auto_propose_entities` inserted the new rows, so the
matcher couldn't see entries that didn't exist yet, and
`record_mentions` then committed without the first-occurrence row.

The pipeline now runs auto-propose *before* `record_mentions` in all
three project-write paths (`translate_segment` miss path,
`_replay_from_cache`, `_commit_group_item`), then computes
first-occurrence spans for the freshly created entries via a new
`_spans_for_new_entries` helper that calls `match_source` against
the actual segment text. The matcher's spans land in
`entity_mention` with proper character offsets, so the Occurrences
modal's `«…»` highlight works on the very first listing. When the
matcher comes back empty — typically because lemma normalization
stripped a particle the source still carries, or the helper LLM
hallucinated a surface form — we fall back to a span-less mention
so the segment still appears in the Occurrences list (just without
an inline highlight). The `segment.translated` event's
`mention_entry_ids` list now includes the proposed entries too, so
the Inbox / event tail surfaces them.

### Added — Glossary "Uses" column and "Show occurrences" modal

Curators reviewing the lore bible asked two practical questions the
Glossary screen couldn't answer: *how often is this entry actually
firing?* and *where is it firing?*. The data was already in the DB
(every translated segment writes `entity_mention` rows), it just
wasn't surfaced.

- **Uses column on the Glossary table.** New rightmost column reads
  `N (Ms)` — total mentions and the distinct segment count. Renders
  `—` for entries with no recorded mentions yet (e.g. proposed entries
  that haven't been re-translated past). Backed by a single
  aggregate query (`repo.count_mentions_per_entry`), so opening the
  screen on a thousand-entry lore bible is still one round trip.
- **`o` → Show occurrences.** New `OccurrencesScreen` modal lists
  every recorded use of the highlighted entry in book order
  (chapter spine_idx → segment idx → span start), with the matched
  span wrapped in `«…»` so the curator can spot the exact sentence
  that fired. Backed by `repo.list_occurrences`, a single
  `entity_mention` ⨝ `segment` ⨝ `chapter` join scoped to the
  current project so a sibling translation's mentions can never
  leak in. Locked Lore Book entries (no project chapters) get a
  graceful empty state.
- **Detail-pane label fix.** The detail pane previously said
  `Mentions: N segment(s)` while actually counting *mention rows*
  (a segment with three matches counted three). It now says
  `N (across M segments)` and points at the new `o` action.

### Changed — Glossary applied per-segment, gender-aware, and dedup-safe

This batch addresses three curator-reported regressions in how the
lore bible flows into the translator prompt and how new entries land
in the project DB. Together they make the glossary "use the term
every time" promise actually hold without over-applying common-noun
mappings (the "House → Câmara" surfaced on plain `house` bug).

- **Per-segment glossary filter** (PRD F-LB-3 / glossary-invariants
  §1). The translator prompt used to ship the *whole* project glossary
  as constraints to every segment, which (a) drowned locked entries
  in noise once the lore bible grew and (b) over-applied common-noun
  entries — the validator would hard-fail "the small house in the
  woods" because the locked `House → Câmara` (a parliamentary
  chamber) source term technically matched. The pipeline now runs
  `match_source` on each segment first and only renders entries with
  an actual word-boundary hit. Cache invalidation still folds the
  full project glossary so a curator edit / cascade clears stale
  translations downstream. The grouped-translator path uses the
  union of relevant entries across the surviving items so a TOC
  batch still gets every locked name it needs.
- **Glossary auto-proposer dedupes by source term, not by
  `(source_term, type)`.** An unreliable helper LLM used to land the
  same proper noun twice (once as `term`, once as `place`) which
  confused the validator and bloated the lore bible. Auto-proposal
  now keys on `source_term` alone, auto-upgrades a generic `term`
  row to a more specific type when the next pass is confident, and
  never overwrites curator-promoted entries. Manual creation via
  `repo.create_glossary_entry` still allows the rare disambiguation
  case where one source spelling really does map to two senses with
  different targets.
- **"Merge duplicates" action on the Glossary screen.** Existing
  duplicates from the old auto-propose contract are surfaced via
  `repo.find_duplicate_source_terms`. Pressing `m` on the Glossary
  screen opens a confirm modal; on `y`, `repo.merge_glossary_entries`
  folds each loser's source/target spellings into the winner's
  aliases, re-points `entity_mention` rows so audit history is
  preserved, deletes the losers, and appends a `glossary_revision`
  + `glossary.duplicates_merged` event. The winner is the highest
  status in the group (locked > confirmed > proposed), specific type
  before `term`, oldest first.
- **Gender flows from the entry to the prompt.** `GlossaryConstraint`
  and `TargetOnlyConstraint` now carry `gender`, `_format_glossary_block`
  renders it inline (`House → Câmara (gender: feminine)`), and the
  translator system prompt has a new hard rule: when a glossary entry
  has a gender, articles, demonstratives, possessives, adjectives,
  and past participles MUST agree with it (so `the House` →
  `a Câmara` / `da Câmara`, never `o Câmara`). When a glossary term
  appears with an article in the source, the article must be kept
  and inflected in the target.
- **"Same-sense" guard rule.** A new hard rule in the system prompt
  asks the model to apply a glossary entry only when the source term
  is used in the *same sense* as the entry — common-noun → specialized
  mappings should be skipped when the source uses the word in an
  ordinary, unrelated sense. The `notes` field is now positioned as
  a "when to apply" hint so curators have a place to disambiguate.

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
