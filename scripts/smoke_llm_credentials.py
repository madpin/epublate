"""Probe a set of OpenAI-compatible LLM credentials against every
LLM channel epublate uses (translator, group translator, extractor,
target extractor).

Usage:

    uv run python scripts/smoke_llm_credentials.py

Per configuration, exercises five real prompt shapes the pipeline
ships in production:

* **Translator** — single segment with placeholders, en→pt; verifies
  the JSON parse and the placeholder round-trip (PRD invariant §2.1).
* **Group translator** — batched short items (PRD F-LLM-2).
* **Extractor (1 paragraph)** — minimal helper-LLM call.
* **Extractor (pre-pass-size)** — ~1500 source tokens of prose to
  reproduce the failure mode that surfaced in the field for reasoning
  helpers (e.g. ``gpt-oss-20b`` on Groq emitting empty visible
  content because reasoning tokens consume the entire visible-channel
  budget).
* **Lore target ingest** — target-language extractor (PRD F-LB-10).

All calls go through :func:`epublate.llm.json_mode.chat_with_json_fallback`
so the soft-fallback path is exercised exactly the way the pipeline
exercises it.

Configuration source (priority order, never hardcoded — keeps API
keys out of git):

1. JSON file at ``$EPUBLATE_SMOKE_CONFIGS``.
2. ``.smoke_configs.json`` in the repo root.

Schema (a JSON array):

    [
        {
            "name": "<display name>",
            "base_url": "https://...",
            "api_key": "sk-...",
            "translator_model": "<slug>",
            "helper_model": "<slug>"
        },
        ...
    ]

Both file paths are gitignored; the script aborts with a clear error
if neither exists. This script makes real network calls and is **not**
part of the ``uv run pytest`` suite (bootstrap contract: no network).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from epublate.errors import LLMResponseError, LLMTransportError
from epublate.llm.base import ResponseFormat
from epublate.llm.json_mode import chat_with_json_fallback
from epublate.llm.openai_compat import OpenAICompatProvider, RetryPolicy
from epublate.llm.prompts.extractor import (
    DEFAULT_RESPONSE_FORMAT,
    build_extractor_messages,
    parse_extractor_response,
)
from epublate.llm.prompts.extractor_target import (
    DEFAULT_RESPONSE_FORMAT as TARGET_DEFAULT_RESPONSE_FORMAT,
)
from epublate.llm.prompts.extractor_target import (
    build_target_extractor_messages,
    parse_target_extractor_response,
)
from epublate.llm.prompts.translator import (
    build_group_translator_messages,
    build_translator_messages,
    parse_group_translator_response,
    parse_translator_response,
)

_PLACEHOLDER_RE = re.compile(r"\[\[T\d+\]\]")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / ".smoke_configs.json"


@dataclass(slots=True, frozen=True)
class Config:
    name: str
    base_url: str
    api_key: str
    translator_model: str
    helper_model: str


def _load_configs() -> list[Config]:
    """Read configs from JSON. Aborts with a friendly error if missing."""

    override = os.environ.get("EPUBLATE_SMOKE_CONFIGS")
    path = Path(override).expanduser() if override else _DEFAULT_CONFIG_PATH
    if not path.is_file():
        sys.exit(
            f"smoke probe: no config file at {path}. "
            "Create it as a JSON array of objects with keys: "
            "name, base_url, api_key, translator_model, helper_model. "
            "Set $EPUBLATE_SMOKE_CONFIGS to override the path. "
            "Both default paths are .gitignored."
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list) or not raw:
        sys.exit(f"smoke probe: {path} must contain a non-empty JSON array.")

    configs: list[Config] = []
    for entry in raw:
        try:
            configs.append(
                Config(
                    name=entry["name"],
                    base_url=entry["base_url"],
                    api_key=entry["api_key"],
                    translator_model=entry["translator_model"],
                    helper_model=entry["helper_model"],
                )
            )
        except (KeyError, TypeError) as exc:
            sys.exit(f"smoke probe: malformed entry in {path}: {exc}")
    return configs


@dataclass(slots=True)
class ProbeResult:
    channel: str
    model: str
    ok: bool = False
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None
    parse_ok: bool = False
    placeholder_round_trip: bool | None = None
    fallback_engaged: bool = False
    sample: str = ""
    notes: list[str] = field(default_factory=list)


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " | ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _provider_for(cfg: Config) -> OpenAICompatProvider:
    """Build a provider with a single attempt — we want fast feedback,
    not a retry storm against a broken endpoint. The 90s timeout is
    generous because reasoning models on free tiers (Nemotron, GPT-5)
    can chew through several thousand reasoning tokens before
    surfacing the visible answer; below ~60s we'd give up on slow
    but otherwise healthy endpoints.

    Honors ``EPUBLATE_LLM_REASONING_EFFORT`` (the same env var the
    real factory honors) so the curator can A/B reasoning-effort
    settings against their config without rebuilding the project.
    """

    reasoning_effort = (
        os.environ.get("EPUBLATE_LLM_REASONING_EFFORT", "").strip() or None
    )
    return OpenAICompatProvider(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        retry_policy=RetryPolicy(max_retries=0),
        timeout=90.0,
        reasoning_effort=reasoning_effort,
    )


def probe_translator(cfg: Config) -> ProbeResult:
    """One translator-style chat: en→pt, single placeholder, JSON mode."""

    source = (
        "[[T0]]Élise[[T0]] opened the door of [[T1]]Vale Verde[[T1]] "
        "and listened to the silence."
    )
    messages = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text=source,
    )
    result = ProbeResult(channel="translator", model=cfg.translator_model)
    started = time.monotonic()
    try:
        chat = chat_with_json_fallback(
            _provider_for(cfg),
            messages,
            model=cfg.translator_model,
            response_format=ResponseFormat(type="json_object"),
            temperature=0.0,
            seed=7,
        )
    except (LLMResponseError, LLMTransportError) as exc:
        result.latency_s = time.monotonic() - started
        result.error = _truncate(str(exc), 240)
        return result
    except Exception as exc:
        result.latency_s = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {_truncate(str(exc), 220)}"
        return result

    result.latency_s = time.monotonic() - started
    result.prompt_tokens = chat.prompt_tokens
    result.completion_tokens = chat.completion_tokens
    result.sample = _truncate(chat.content, 220)
    if not chat.content.strip():
        result.notes.append(
            "empty visible content — reasoning model exhausted its budget?"
        )
        return result

    result.ok = True
    try:
        parsed = parse_translator_response(chat.content)
    except LLMResponseError as exc:
        result.notes.append(f"parse failed: {_truncate(str(exc), 120)}")
        return result

    result.parse_ok = True
    expected = sorted(_PLACEHOLDER_RE.findall(source))
    actual = sorted(_PLACEHOLDER_RE.findall(parsed.target))
    result.placeholder_round_trip = expected == actual
    if not result.placeholder_round_trip:
        result.notes.append(f"placeholders src={expected} tgt={actual}")
    return result


def probe_group_translator(cfg: Config) -> ProbeResult:
    """One grouped-translator chat: short batched items, JSON map out.

    Exercises the batch-cost-saver path (PRD F-LLM-2) — many short
    placeholder-free segments are shipped in a single request.
    """

    items = [
        (1, "The hall was empty."),
        (2, "Élise listened."),
        (3, "Snow began to fall."),
        (4, "She did not move."),
    ]
    messages = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=items,
    )
    result = ProbeResult(channel="group translator", model=cfg.translator_model)
    started = time.monotonic()
    try:
        chat = chat_with_json_fallback(
            _provider_for(cfg),
            messages,
            model=cfg.translator_model,
            response_format=ResponseFormat(type="json_object"),
            temperature=0.0,
            seed=7,
        )
    except (LLMResponseError, LLMTransportError) as exc:
        result.latency_s = time.monotonic() - started
        result.error = _truncate(str(exc), 240)
        return result
    except Exception as exc:
        result.latency_s = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {_truncate(str(exc), 220)}"
        return result

    result.latency_s = time.monotonic() - started
    result.prompt_tokens = chat.prompt_tokens
    result.completion_tokens = chat.completion_tokens
    result.sample = _truncate(chat.content, 220)
    if not chat.content.strip():
        result.notes.append("empty visible content")
        return result

    result.ok = True
    try:
        trace = parse_group_translator_response(
            chat.content, expected_ids=[i for i, _ in items]
        )
    except LLMResponseError as exc:
        result.notes.append(f"parse failed: {_truncate(str(exc), 120)}")
        return result

    result.parse_ok = True
    result.notes.append(
        f"translations returned: {len(trace.translations)} (expected 4)"
    )
    return result


def probe_extractor(cfg: Config) -> ProbeResult:
    """One extractor-style helper chat: paragraph in, entity JSON out."""

    source = (
        "The Order of the Coffer met at dawn in Vale Verde. "
        "Élise carried the lantern; her brother Henrik followed, silent."
    )
    messages = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text=source,
    )
    result = ProbeResult(channel="extractor (helper)", model=cfg.helper_model)
    started = time.monotonic()
    try:
        chat = chat_with_json_fallback(
            _provider_for(cfg),
            messages,
            model=cfg.helper_model,
            response_format=DEFAULT_RESPONSE_FORMAT,
            temperature=0.0,
            seed=7,
        )
    except (LLMResponseError, LLMTransportError) as exc:
        result.latency_s = time.monotonic() - started
        result.error = _truncate(str(exc), 240)
        return result
    except Exception as exc:
        result.latency_s = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {_truncate(str(exc), 220)}"
        return result

    result.latency_s = time.monotonic() - started
    result.prompt_tokens = chat.prompt_tokens
    result.completion_tokens = chat.completion_tokens
    result.sample = _truncate(chat.content, 220)
    if not chat.content.strip():
        result.notes.append(
            "empty visible content — reasoning helper exhausted its budget?"
        )
        return result

    result.ok = True
    try:
        trace = parse_extractor_response(chat.content)
    except LLMResponseError as exc:
        result.notes.append(f"parse failed: {_truncate(str(exc), 120)}")
        return result

    result.parse_ok = True
    result.notes.append(f"entities returned: {len(trace.entities)}")
    return result


def probe_extractor_large(cfg: Config) -> ProbeResult:
    """Helper extractor under realistic pre-pass load.

    The user's reported failure (``extractor response was empty`` /
    ``json_validate_failed``) was on full-chapter pre-pass chunks, not
    one-paragraph snippets. This probe ships ~1500 source tokens —
    representative of one ``run_pre_pass`` chunk — so a reasoning
    helper that exhausts its visible-channel budget on reasoning
    tokens fails *here*, not just on a real book.
    """

    paragraphs = [
        "The Order of the Coffer met at dawn in Vale Verde. The chapel "
        "smelled of wet pine and old wax, and the old verger Anselmo "
        "stood waiting in the nave, his lamp turned low.",
        "Élise carried the lantern up the granite stair; her brother "
        "Henrik followed, silent. They had not spoken since Brindis, "
        "and the silence between them weighed more than the lantern.",
        "At the top, Mestre Cordeiro waited with the iron key. He had "
        "promised the Inquisidor of Sao Bras nothing of what they would "
        "find here — but Cordeiro had broken larger promises in his time.",
        "The reliquary was empty. Of course it was empty. Henrik laughed "
        "once, a short bark in the cold dark, and Élise put her hand on "
        "his arm to silence him. Cordeiro lifted the lantern higher.",
        "On the floor of the reliquary lay a single brass token, "
        "stamped with the sigil of the Casa do Lobo. Élise knew the "
        "sigil from the Saint's Day fair in Coimbra, when she had been "
        "twelve and the world had still been small.",
        "It was the first proof, then. Cordeiro pocketed the token "
        "without a word. Outside, the bells of Vale Verde began to "
        "ring for matins, and the three of them descended in silence "
        "to a colder morning than they had climbed in.",
    ]
    messages = build_extractor_messages(
        source_lang="en",
        target_lang="pt",
        source_text="\n\n".join(paragraphs),
    )
    result = ProbeResult(channel="extractor (pre-pass-size)", model=cfg.helper_model)
    started = time.monotonic()
    try:
        chat = chat_with_json_fallback(
            _provider_for(cfg),
            messages,
            model=cfg.helper_model,
            response_format=DEFAULT_RESPONSE_FORMAT,
            temperature=0.0,
            seed=7,
        )
    except (LLMResponseError, LLMTransportError) as exc:
        result.latency_s = time.monotonic() - started
        result.error = _truncate(str(exc), 240)
        return result
    except Exception as exc:
        result.latency_s = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {_truncate(str(exc), 220)}"
        return result

    result.latency_s = time.monotonic() - started
    result.prompt_tokens = chat.prompt_tokens
    result.completion_tokens = chat.completion_tokens
    result.sample = _truncate(chat.content, 220)
    if not chat.content.strip():
        result.notes.append(
            "empty visible content — reasoning helper exhausted its budget"
        )
        return result

    result.ok = True
    try:
        trace = parse_extractor_response(chat.content)
    except LLMResponseError as exc:
        result.notes.append(f"parse failed: {_truncate(str(exc), 120)}")
        return result

    result.parse_ok = True
    result.notes.append(f"entities returned: {len(trace.entities)}")
    return result


def probe_lore_target(cfg: Config) -> ProbeResult:
    """One target-language extractor chat for Lore Book ingest.

    Mirrors :func:`epublate.lore.ingest_target.ingest_target_epub`'s
    helper call shape — same direction the curator runs when ingesting
    a translated edition of a series.
    """

    target_text = (
        "A Ordem do Cofre reuniu-se ao amanhecer em Vale Verde. "
        "Élise levava a lanterna pela escada de granito; o irmão "
        "Henrik seguia em silêncio. No alto, Mestre Cordeiro "
        "esperava com a chave de ferro."
    )
    messages = build_target_extractor_messages(
        target_lang="pt",
        target_text=target_text,
    )
    result = ProbeResult(channel="lore target ingest", model=cfg.helper_model)
    started = time.monotonic()
    try:
        chat = chat_with_json_fallback(
            _provider_for(cfg),
            messages,
            model=cfg.helper_model,
            response_format=TARGET_DEFAULT_RESPONSE_FORMAT,
            temperature=0.0,
            seed=7,
        )
    except (LLMResponseError, LLMTransportError) as exc:
        result.latency_s = time.monotonic() - started
        result.error = _truncate(str(exc), 240)
        return result
    except Exception as exc:
        result.latency_s = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {_truncate(str(exc), 220)}"
        return result

    result.latency_s = time.monotonic() - started
    result.prompt_tokens = chat.prompt_tokens
    result.completion_tokens = chat.completion_tokens
    result.sample = _truncate(chat.content, 220)
    if not chat.content.strip():
        result.notes.append("empty visible content")
        return result

    result.ok = True
    try:
        trace = parse_target_extractor_response(chat.content)
    except LLMResponseError as exc:
        result.notes.append(f"parse failed: {_truncate(str(exc), 120)}")
        return result

    result.parse_ok = True
    result.notes.append(f"entities returned: {len(trace.entities)}")
    return result


def _verdict(r: ProbeResult) -> str:
    if r.error:
        return "FAIL"
    if not r.ok:
        return "FAIL (empty content)"
    if not r.parse_ok:
        return "PARTIAL (parser rejected)"
    if r.placeholder_round_trip is False:
        return "PARTIAL (placeholders broken)"
    return "OK"


def _emit(r: ProbeResult) -> None:
    verdict = _verdict(r)
    lines = [
        f"  [{verdict:30s}] {r.channel:20s} model={r.model}",
        f"      latency={r.latency_s:.2f}s "
        f"tokens=(in={r.prompt_tokens}, out={r.completion_tokens})",
    ]
    if r.error:
        lines.append(f"      error: {r.error}")
    if r.sample and not r.error:
        lines.append(f"      sample: {r.sample}")
    for note in r.notes:
        lines.append(f"      note:   {note}")
    print("\n".join(lines))


def main() -> int:
    configs = _load_configs()
    print("epublate :: smoke_llm_credentials")
    print("==================================")
    overall_ok = True
    for cfg in configs:
        print()
        print(f"=== {cfg.name} ===")
        print(f"    base_url={cfg.base_url}")
        probes = [
            probe_translator(cfg),
            probe_group_translator(cfg),
            probe_extractor(cfg),
            probe_extractor_large(cfg),
            probe_lore_target(cfg),
        ]
        for r in probes:
            _emit(r)
        if any(_verdict(r).startswith("FAIL") for r in probes):
            overall_ok = False
    print()
    print(
        "=== summary === "
        + ("all probes succeeded" if overall_ok else "one or more probes failed")
    )
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
