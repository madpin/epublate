# Agent Guide — epublate

This file is the canonical, cross-tool agent briefing for this
repository. It is read by Cursor, Claude Code, OpenAI Codex, Aider, and
similar agents. `CLAUDE.md` is a symlink to this file.

> **The authoritative product spec is [`docs/PRD.md`](docs/PRD.md).**
> When this file and the PRD disagree, the PRD wins. Update both if a
> design decision changes.

---

## 1. What this project is

`epublate` is a **rich TUI** application that translates ePub story
books with an **LLM**, while:

- preserving the ePub's structure and inline formatting end-to-end,
- maintaining a per-project **lore bible** (characters, places,
  events, dates, items, organizations, recurring phrases) so that
  proper-noun translations are **consistent across the entire book**,
- being fully **resumable** — every LLM call, decision, and edit is
  persisted in a single SQLite file per project.

PDF support is a **non-goal in v1** but the format-handling layer is
designed to make it pluggable later.

## 2. Hard invariants — do not violate these

Listed in priority order. If a change would weaken any of these, stop
and ask the user.

1. **Format preservation.** Every ePub the app produces must be valid
   and readable. Inline tags (`<em>`, `<strong>`, `<a>`, `<ruby>`,
   footnote markers) must round-trip exactly. The model never sees raw
   HTML — inline tags are replaced with opaque placeholders
   (`[[T0]]…[[T0]]`) before the call and restored after. The validator
   must confirm every placeholder appears exactly once in the target.
2. **Glossary consistency.** A `locked` glossary entry is
   **non-negotiable**. The validator hard-fails any segment whose
   source contains a locked source-term but whose target does not use
   the canonical target-term. `confirmed` entries are warnings.
   `proposed` entries are suggestions only.
3. **Resumability.** Every state-changing action goes through the DB
   in a single transaction. The app must be safe to crash and reopen
   at any point with no corruption. SQLite is in WAL mode. ePub
   exports are atomic (write-temp-then-rename).
4. **Local-first & private.** Book contents leave the machine **only**
   via the LLM/embedding endpoints the user configured. No analytics,
   no telemetry, no auto-uploads.
5. **OpenAI-compatible LLMs only (v1).** All LLM access goes through
   the OpenAI-compatible chat-completions schema. The user supplies
   `base_url` + `api_key` + `model`. Do not add provider-specific code
   paths in v1.
6. **`uv` is the only project tool.** See §5.

## 3. Source of truth, by topic

| Topic                                | Where to look                                       |
| ------------------------------------ | --------------------------------------------------- |
| Product scope, goals, non-goals      | `docs/PRD.md` §§1–3                                 |
| Functional requirements              | `docs/PRD.md` §4                                    |
| Non-functional requirements          | `docs/PRD.md` §5                                    |
| Architecture, modules, schema        | `docs/PRD.md` §6                                    |
| Translation pipeline phases          | `docs/PRD.md` §7                                    |
| Prompting strategy                   | `docs/PRD.md` §8                                    |
| Roadmap & milestones                 | `docs/PRD.md` §10                                   |
| Open questions                       | `docs/PRD.md` §11                                   |

## 4. Planned repository layout

```
epublate/
  pyproject.toml         # PEP 621 metadata + deps; single source of truth
  uv.lock                # committed, reproducible installs
  .python-version        # pinned Python; uv installs it on demand
  src/epublate/
    app/                 # Textual UI (screens, widgets)
    core/                # project, segmentation, pipeline, validators, cache
    formats/             # base.py + epub.py (pdf.py later)
    llm/                 # base.py + openai_compat.py + prompts/
    glossary/            # models, matcher, enforcer
    embeddings/          # base.py + openai_compat.py + local.py (optional)
    db/                  # schema.py + migrations/
    cli.py               # `epublate ...` entry points
  tests/                 # pytest suite, fixtures, snapshot baselines
  docs/PRD.md            # the spec
  AGENTS.md              # this file
  CLAUDE.md              # symlink → AGENTS.md
  README.md
```

The repo is currently a skeleton (only `README.md`, `LICENSE`,
`.gitignore`, `docs/PRD.md`, and this file). Build it out per
milestone M0 in the PRD.

## 5. Tooling — `uv` only

`uv` is the canonical tool for environments, dependencies, running
tests/linters, and building/publishing. **Do not** introduce `pip`,
`pipx`, `poetry`, `pyenv`, `tox`, `virtualenv`, `setup.py`,
`setup.cfg`, or `requirements.txt`.

```bash
uv python install                       # install the right Python
uv sync --all-extras --dev              # create venv + install everything
uv run pytest                           # tests (no network, no LLM keys)
uv run ruff check .                     # lint
uv run ruff format --check .            # format check
uv run mypy src/epublate                # types
uv run epublate --mock-llm ...          # run the app
uv build                                # wheel + sdist
uv tool install epublate                # end-user install (after release)
uvx epublate ...                        # ephemeral run (after release)
```

The bootstrap contract is: **on a fresh clone, `uv sync --all-extras
--dev && uv run pytest` must pass with zero other prerequisites and
zero network access.** CI must run exactly the same commands.

## 6. Coding conventions

- **Python 3.13+**, typed top-to-bottom. `mypy --strict` on the core.
- **Layout:** `src/`-layout package. No top-level package alongside
  tests.
- **Lint/format:** `ruff` (lint + format). No `black`, no `isort`
  separately — `ruff` handles both.
- **Typed I/O:** `pydantic` for configs, LLM request/response shapes,
  and any data crossing module boundaries.
- **DB access:** SQLAlchemy Core (not ORM unless a clear win).
  Migrations via Alembic. Always use parameterized queries.
- **Async:** Textual workers handle long operations. UI thread is
  never blocked.
- **Logging:** structured logging via `logging` + `rich`. No `print`
  in library code.
- **Errors:** raise typed exceptions; never swallow; LLM failures are
  retryable and recorded in `llm_call`.
- **Comments:** explain *why*, not *what*. No narration comments.

## 7. Testing conventions

- **`pytest` is the only runner.** Run via `uv run pytest`.
- **No network in tests.** Use the deterministic mock LLM provider
  (`epublate.llm.mock`) — see PRD §6.1 / NFR-7.
- **Property-based tests** (`hypothesis`) for segmentation and
  reassembly: `assemble(segment(doc)) == doc` for any well-formed
  XHTML.
- **Snapshot tests** (`pytest-textual-snapshot`) for TUI screens.
- **Fixtures** live in `tests/fixtures/` — keep ePubs there small and
  CC0-licensed.
- **Coverage target:** 85% line coverage on `core/`, `formats/`,
  `glossary/`, and `llm/`. UI code has lower coverage but must have
  snapshot tests.

## 8. LLM and cost discipline

- Every LLM call goes through the provider interface and gets
  recorded in `llm_call` (tokens, cost, cache hit).
- Cache key: `(model, system_prompt_hash, user_prompt_hash,
  glossary_state_hash)`. Cache hits never make a network call.
- Concurrency defaults to 1; user-configurable.
- Per-project budget cap pauses batch mode and prompts the user.

## 9. Things agents should never do

- Modify `LICENSE` or change the license terms.
- Add provider-specific LLM code (e.g., Anthropic-only, Gemini-only)
  in v1. Use only the OpenAI-compatible schema.
- Introduce alternative package managers or build systems.
- Bypass the validator to "make a test pass". If a translation
  violates a locked glossary entry, the right answer is to fix the
  prompt or the entry, not the validator.
- Send book contents anywhere except the configured LLM/embedding
  endpoints.
- Commit `uv.lock` changes without also updating `pyproject.toml`
  intentionally.
- Add comments that narrate code (e.g., `# increment counter`).
- Land features that aren't traceable to a PRD requirement (FR / NFR
  ID) or an explicit open-question resolution.

## 10. Working with the PRD

- The PRD is a living document. When you make a design decision that
  resolves an entry from `docs/PRD.md` §11 (Open Questions), move it
  out of §11 and into the matching functional/non-functional section
  in the same PR.
- New functional requirements get a stable ID (`F-…`, `NFR-…`) and
  are referenced from commit messages and PR descriptions.
- Roadmap entries (M0–M6) are checked off as their acceptance
  criteria are met.

## 11. Quick checklist before opening a PR

- [ ] `uv sync --all-extras --dev && uv run pytest` passes locally.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] `uv run mypy src/epublate` clean.
- [ ] Touched code is covered by a test (unit, property, or
      snapshot).
- [ ] PRD updated if a requirement, invariant, or open question
      changed.
- [ ] No new dependency without a one-line justification in the PR
      description.
- [ ] No `print`, no `TODO` without an issue link, no commented-out
      code.
