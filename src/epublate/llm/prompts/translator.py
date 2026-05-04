"""Translator prompt builder + response parser (PRD §8.1, §8.4 / F-LLM-3).

Pure, side-effect-free functions: the pipeline owns the LLM call and the
DB write; this module only turns project state into ``Message`` objects
and turns the model's response back into a typed ``TranslatorTrace``.

Inline tags never reach the model — :func:`build_translator_messages`
assumes the caller has already replaced them with ``[[T0]]…[[/T0]]``
placeholders via :mod:`epublate.core.segmentation` (format-handling rule).

The system prompt deliberately encodes:

* the placeholder-discipline contract (every issued placeholder must come
  back exactly once, no extras, no drops),
* the JSON response shape the parser expects,
* hard / soft glossary constraints (M3 plugs in real entries; M2 sends an
  empty list).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from epublate.errors import LLMResponseError
from epublate.llm.base import Message

GlossaryStatus = Literal["proposed", "confirmed", "locked"]
GenderTag = Literal["feminine", "masculine", "neuter", "common", "unspecified"]
"""Mirror of :data:`epublate.glossary.models.GenderTag`.

Re-declared here to keep the prompt module free of cross-package
imports (`translator.py` is intentionally above `glossary/` in the
dependency graph). When the glossary projects an entry into a
:class:`GlossaryConstraint`, gender is pinned through verbatim so the
prompt can ask the LLM to match articles and agreement to it (e.g.
"a Câmara dos Lordes" / "do Senhor da Casa", not "o Câmara").
"""


class GlossaryConstraint(BaseModel):
    """One glossary constraint as it lands in the translator's system prompt.

    Kept independent of :mod:`epublate.glossary.models` (which arrives in
    M3) so M2 can produce empty lists without circular imports.

    ``gender`` is surfaced inline next to the term so the LLM can
    match articles and agreement (PRD §4.3 / glossary-invariants
    rule §6) — gendered target languages need this for natural-sounding
    phrases like "a Câmara dos Lordes" rather than "o Câmara".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_term: str
    target_term: str
    type: str = "term"
    status: GlossaryStatus = "confirmed"
    notes: str | None = None
    gender: GenderTag | None = None


class TargetOnlyConstraint(BaseModel):
    """One target-only constraint (PRD §4.3 / F-LB-9).

    Carries the canonical *target* spelling for an entity whose
    source-side wording is unknown to the curator. The translator is
    asked to infer the source term in-segment and use the canonical
    target form when it maps. Soft-locked: a missed match is a warning,
    not a hard failure (validator-side enforcement lives in
    :func:`epublate.glossary.enforcer.validate_target`).

    Like :class:`GlossaryConstraint`, ``gender`` flows through to the
    prompt so the LLM picks the right article / agreement when the
    canonical target form has a grammatical gender.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_term: str
    type: str = "term"
    status: GlossaryStatus = "confirmed"
    notes: str | None = None
    target_aliases: tuple[str, ...] = ()
    gender: GenderTag | None = None


class TranslatorTrace(BaseModel):
    """Parsed translator response (PRD §8.1).

    ``new_entities`` is intentionally typed loosely (a list of dicts) — the
    extractor pipeline (M5) defines the canonical shape; this parser just
    forwards whatever the model returned for downstream review.
    """

    model_config = ConfigDict(extra="forbid")

    target: str
    used_entries: list[str] = Field(default_factory=list)
    new_entities: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class GroupTranslatorItem(BaseModel):
    """One item in a grouped translator response.

    The grouped call batches many short, placeholder-free segments (e.g.
    a table of contents, a glossary, a long list) into a single request
    so we amortize one LLM round-trip over dozens of segments. Each
    item still needs its own ``target`` and optional glossary trace so
    the pipeline can validate and persist them independently.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    target: str
    used_entries: list[str] = Field(default_factory=list)
    new_entities: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class GroupTranslatorTrace(BaseModel):
    """Parsed grouped translator response."""

    model_config = ConfigDict(extra="forbid")

    translations: list[GroupTranslatorItem]
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ContextSegment:
    """One preceding segment surfaced to the translator as context.

    The pipeline assembles a list of these from the same chapter, in
    book order, capped by the user's
    :class:`epublate.core.pipeline.ContextOptions` knobs. Each carries
    the *raw* source text (placeholders left in place) plus, when
    available, the curator-approved target text. ``segments_back``
    is the 1-based distance from the segment under translation
    (``1`` = immediately preceding, ``2`` = two before, …) so the
    prompt can render a stable, oldest-first ordering.
    """

    source_text: str
    target_text: str | None
    segments_back: int


_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary translator working on a long ePub story book.

Translate the user's source segment from {source_lang} to {target_lang}.

Hard rules — these are not negotiable:

1. Inline formatting is encoded as opaque placeholders of the form
   `[[T0]]`, `[[/T0]]`, `[[T1]]`, etc. Every placeholder that appears in
   the source MUST appear exactly once in your translation, in the same
   relative order. Do not invent new placeholders. Do not drop any.
   Closing placeholders (`[[/T0]]`) must always pair with their opener
   (`[[T0]]`).
2. Translate naturally for the target audience but preserve narrative
   voice, tense, and POV. Do not paraphrase past the meaning of the
   source. Do not summarize. Render punctuation, contractions, and
   orthography according to target-language conventions —
   apostrophes, quotation mark style, dash usage, and similar
   typographic patterns are language-specific, so do NOT mechanically
   transcribe source-side punctuation that has no equivalent in the
   target. When this run's source/target pair has known pitfalls,
   they are listed under "Language-pair notes" below.
3. Translate every textual passage end-to-end. Embedded quotations
   (even when wrapped in placeholder pairs that mark italics or
   blockquote runs), bracketed asides like ``[sic]`` / ``[Emphasis
   added]``, parenthetical clauses, footnote text, and book / article
   titles cited in the prose are all part of the segment and MUST be
   translated. Do not leave any chunk in the source language to
   "preserve the original quote" unless it is a code snippet or a
   proper name. If you would normally render a cited title as a
   parallel-text bilingual quote, instead translate it inline like
   the rest of the text and let the curator add a footnote later.
4. Preserve the leading and trailing whitespace of the source segment
   verbatim. If the source begins with newlines and indentation
   (e.g. ``"\\n    [[T0]]…"``) your target MUST begin with the same
   characters; same for trailing whitespace. Do not strip, collapse,
   or "tidy" the surrounding whitespace.
5. Keep proper nouns, place names, and domain terms consistent across
   the book. The glossary below lists agreed translations.
6. Locked glossary entries are non-negotiable. Confirmed entries are
   strong defaults. Proposed entries are suggestions.
7. Apply a glossary entry only when the source term is used in the
   same sense as the entry. Some entries map a common noun to a
   specialized translation (e.g. ``House`` → ``Câmara`` for a
   parliamentary chamber) — when the source uses the same word in an
   ordinary, unrelated sense (a building, a family, …), translate
   it idiomatically and ignore the entry. The notes column on each
   entry, when present, hints at the intended sense.
8. When a glossary entry carries a ``(gender: …)`` marker the
   canonical target term has that grammatical gender. Surrounding
   articles, demonstratives, possessives, adjectives, and past
   participles MUST agree with that gender, including any preposition
   contractions (e.g. ``a Câmara`` / ``da Câmara`` for feminine,
   ``o Senhor`` / ``do Senhor`` for masculine).
   When the source uses an article with a glossary term, your
   translation MUST keep the article and inflect it correctly.
9. Glossary entries are recorded in a balanced shape: either both
   the source term and the target term carry a leading article /
   preposition (e.g. ``the USA → os EUA``), or neither does
   (``Europe → Europa``, ``USA → EUA``). When the entry has NO
   leading article, you MUST inflect the surrounding article /
   preposition / contraction yourself based on the source: render
   ``"in Europe"`` as ``"na Europa"``, ``"the Senate voted"`` as
   ``"o Senado votou"``, etc. — and never emit doubled function
   words like ``"na na Europa"`` or ``"the the Senate"``. When the
   entry HAS a leading article on both sides, treat the article as
   part of the canonical spelling and do not add another one.

{language_notes_block}{style_guide_block}{glossary_block}{target_only_block}{context_block}\
Respond with a single JSON object and nothing else:

{{
  "target": "<translated text with placeholders preserved>",
  "used_entries": ["<source_term you used a glossary entry for>", ...],
  "new_entities": [
    {{"type": "character|place|...",
     "source": "<surface form in the source>",
     "target": "<the exact target spelling you used in the translation above>",
     "evidence": "..."}},
    ...
  ],
  "notes": "optional free-text notes for the curator, or omit"
}}

When you list a candidate in ``new_entities`` its ``target`` MUST be
the literal spelling you used inside ``target`` for this segment —
that's how the lore bible learns the canonical translation. If for
some reason the entity does not appear in the translation (e.g. you
elided it), set ``target`` to the form you would use next time.

Do NOT propose raw year references (``1066``, ``1939-1945``,
``1990s``, ``c. 1066``, ``45 BC``) in ``new_entities``. Plain dates
are handled inline by the translation; the lore bible only tracks
*named* eras and recurring holidays (``the Long Night``, ``Yule``).
When a year is part of a longer named phrase the phrase as a whole
is fine (``Year of the Four Emperors``, ``Battle of 1066``).

Do not wrap the JSON in code fences. Do not add commentary.\
"""


_GROUP_SYSTEM_PROMPT_TEMPLATE = """\
You are a literary translator working on a long ePub story book.

The user is sending a BATCH of short, independent segments (typically
items from a table of contents, glossary, list, index, or other
repetitive structure) so you can translate them in a single round-trip.
Translate each segment from {source_lang} to {target_lang}.

Hard rules — these are not negotiable:

1. Preserve the number and order of items. Your response's
   ``translations`` array must contain exactly one entry per input
   item, with the same ``id`` values — do not merge, drop, or invent
   items.
2. Keep each translation scoped to its own item. Do NOT bleed context
   from one item into the next.
3. Translate naturally for the target audience; do not paraphrase past
   the meaning of the source and do not summarize. Render punctuation,
   contractions, and orthography according to target-language
   conventions — apostrophes, quotation mark style, dash usage, and
   similar typographic patterns are language-specific, so do NOT
   mechanically transcribe source-side punctuation that has no
   equivalent in the target. When this run's source/target pair has
   known pitfalls, they are listed under "Language-pair notes" below.
4. Translate every textual passage end-to-end. Embedded quotations,
   bracketed asides, parenthetical clauses, footnote text, and
   book / article titles cited inside an item are all part of that
   item and MUST be translated. Do not leave any chunk in the source
   language to "preserve the original quote" unless it is a code
   snippet or a proper name.
5. Preserve the leading and trailing whitespace of each source item
   verbatim in the matching target. Do not strip, collapse, or
   "tidy" surrounding whitespace.
6. Keep proper nouns, place names, and domain terms consistent with
   the glossary below. Locked glossary entries are non-negotiable,
   confirmed entries are strong defaults, proposed entries are
   suggestions.
7. Apply a glossary entry only when the source term is used in the
   same sense as the entry. When the source uses the same word in an
   ordinary, unrelated sense, translate it idiomatically and ignore
   the entry. The notes column on each entry, when present, hints at
   the intended sense.
8. When a glossary entry carries a ``(gender: …)`` marker the
   canonical target term has that grammatical gender. Surrounding
   articles, demonstratives, possessives, adjectives, and past
   participles MUST agree with that gender, including any preposition
   contractions. When the source uses an article with a glossary
   term, your translation MUST keep the article and inflect it
   correctly.
9. Glossary entries are recorded in a balanced shape: either both
   the source term and the target term carry a leading article /
   preposition (``the USA → os EUA``), or neither does
   (``Europe → Europa``). When the entry has NO leading article,
   inflect the surrounding article / preposition / contraction
   yourself based on the source ("in Europe" → "na Europa", "the
   Senate voted" → "o Senado votou") and never emit doubled
   function words like ``"na na Europa"`` or ``"the the Senate"``.
   When the entry HAS a leading article on both sides, treat the
   article as part of the canonical spelling and do not add another
   one.
10. Inline formatting in each item's source is encoded as opaque
    placeholders of the form ``[[T0]]``, ``[[/T0]]``, ``[[T1]]``, etc.
    For each item, every placeholder that appears in that item's
    source MUST appear exactly once in that item's target, in the same
    relative order. Do not invent new placeholders. Do not drop any.
    Closing placeholders (``[[/T0]]``) must always pair with their
    opener (``[[T0]]``). Placeholder ids are local to each item — do
    not share or shift them across items.

{language_notes_block}{style_guide_block}{glossary_block}{target_only_block}\
Input format: the user message is a JSON object of the shape
``{{"items": [{{"id": 1, "source": "..."}}, ...]}}``. Respond with a
single JSON object and nothing else:

{{
  "translations": [
    {{
      "id": <the item id you were given>,
      "target": "<translated text with placeholders preserved>",
      "used_entries": ["<source_term you applied from the glossary>", ...],
      "new_entities": [
        {{"type": "character|place|...",
         "source": "<surface form in the source>",
         "target": "<the exact target spelling you used above>",
         "evidence": "..."}},
        ...
      ],
      "notes": "optional, omit when empty"
    }},
    ...
  ],
  "notes": "optional batch-level note, omit when empty"
}}

Do NOT propose raw year references (``1066``, ``1939-1945``,
``1990s``, ``c. 1066``, ``45 BC``) in ``new_entities``. Plain dates
are handled inline by the translation; the lore bible only tracks
*named* eras and recurring holidays.

Do not wrap the JSON in code fences. Do not add commentary.\
"""


_LANGUAGE_NAMES: dict[str, str] = {
    # Western European
    "en": "English",
    "en-us": "American English",
    "en-gb": "British English",
    "en-au": "Australian English",
    "en-ca": "Canadian English",
    "fr": "French",
    "fr-fr": "French (France)",
    "fr-ca": "Canadian French",
    "fr-be": "Belgian French",
    "fr-ch": "Swiss French",
    "es": "Spanish",
    "es-es": "European Spanish",
    "es-mx": "Mexican Spanish",
    "es-ar": "Argentine Spanish",
    "es-co": "Colombian Spanish",
    "es-419": "Latin American Spanish",
    "pt": "Portuguese",
    "pt-pt": "European Portuguese",
    "pt-br": "Brazilian Portuguese",
    "it": "Italian",
    "it-it": "Italian",
    "de": "German",
    "de-de": "German",
    "de-at": "Austrian German",
    "de-ch": "Swiss German",
    "nl": "Dutch",
    "nl-nl": "Dutch",
    "nl-be": "Flemish",
    "ca": "Catalan",
    "gl": "Galician",
    "eu": "Basque",
    # Northern European
    "no": "Norwegian",
    "nb": "Norwegian Bokmål",
    "nn": "Norwegian Nynorsk",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "is": "Icelandic",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    # Eastern European
    "ru": "Russian",
    "uk": "Ukrainian",
    "be": "Belarusian",
    "pl": "Polish",
    "cs": "Czech",
    "sk": "Slovak",
    "hu": "Hungarian",
    "ro": "Romanian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sr": "Serbian",
    "sr-latn": "Serbian (Latin)",
    "sr-cyrl": "Serbian (Cyrillic)",
    "sl": "Slovenian",
    "mk": "Macedonian",
    "sq": "Albanian",
    "el": "Greek",
    # Middle East / RTL
    "ar": "Arabic",
    "ar-eg": "Egyptian Arabic",
    "ar-sa": "Saudi Arabic",
    "ar-lb": "Lebanese Arabic",
    "he": "Hebrew",
    "fa": "Persian",
    "ur": "Urdu",
    "ps": "Pashto",
    "ku": "Kurdish",
    "tr": "Turkish",
    "az": "Azerbaijani",
    "hy": "Armenian",
    "ka": "Georgian",
    # South Asian
    "hi": "Hindi",
    "bn": "Bengali",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "mr": "Marathi",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "si": "Sinhala",
    "ne": "Nepali",
    # East Asian
    "zh": "Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "zh-hk": "Hong Kong Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "mn": "Mongolian",
    # Southeast Asian
    "id": "Indonesian",
    "ms": "Malay",
    "vi": "Vietnamese",
    "th": "Thai",
    "lo": "Lao",
    "km": "Khmer",
    "my": "Burmese",
    "tl": "Tagalog",
    "fil": "Filipino",
    # African
    "sw": "Swahili",
    "am": "Amharic",
    "yo": "Yoruba",
    "ha": "Hausa",
    "ig": "Igbo",
    "zu": "Zulu",
    "xh": "Xhosa",
    "af": "Afrikaans",
    # Constructed / classical
    "la": "Latin",
    "eo": "Esperanto",
}
"""BCP-47 → human-readable language name table.

Used to render the prompt's source/target labels as
``French (fr)`` / ``Brazilian Portuguese (pt-BR)`` instead of the bare
code, so the LLM has both an unambiguous language identifier and the
common name. Lookup is case-insensitive on the full tag first, then
falls back to the primary subtag (``pt`` for ``pt-BR``) so a one-line
addition covers every regional variant of a given language. Adding a
language is a single key/value pair — keep entries short, capitalize
the same way the language is conventionally written in English.

Why a hand-rolled dict (no ``babel`` / locale dependency): we want a
single source of truth for the prompt that we can curate (e.g. spell
``Brazilian Portuguese`` rather than the more clinical
``Portuguese (Brazil)``), and we want to be able to ship without an
extra dependency. The PRD's local-first invariant is satisfied
trivially by a dict; ``babel`` would require a 30+ MB locale corpus
for a 100-line lookup.
"""


def _format_language_label(lang: str) -> str:
    """Render a language code as ``Name (code)`` — or just the code.

    Falls back to the full tag (``pt-BR``) when no name is registered
    for either the full tag or the primary subtag. The output always
    includes the original code so the LLM can disambiguate dialect
    variants the prose name might glide over (e.g. ``en-US`` vs
    ``en-GB`` both read as "English" in conversation but cue different
    spelling conventions).
    """

    if not lang:
        return lang
    raw = lang.strip()
    if not raw:
        return raw
    key = raw.lower()
    name = _LANGUAGE_NAMES.get(key)
    if name is None:
        primary = key.split("-", 1)[0]
        if primary != key:
            name = _LANGUAGE_NAMES.get(primary)
    if name is None:
        return raw
    return f"{name} ({raw})"


_SOURCE_LANG_NOTES: dict[str, str] = {
    "fr": (
        "French uses apostrophe contractions for elision "
        "(``j'avais``, ``s'appelait``, ``l'arbre``, ``qu'il``, "
        "``d'avoir``, ``n'est``). The apostrophe is part of French "
        "orthography ONLY — when the target language does not use "
        "the same convention, the apostrophe MUST be removed entirely "
        "(NOT moved, NOT preserved as a leading character on the "
        "next word). Translate the elided forms into their full "
        "target-language equivalents:\n"
        "  - ``j'avais`` → ``eu tinha`` (NOT ``eu'tinha``, NOT "
        "``eu 'tinha``).\n"
        "  - ``J'ai une excuse.`` → ``Tenho uma desculpa.`` (NOT "
        "``Eu 'tenho uma desculpa.``).\n"
        "  - ``s'appelait`` → ``se chamava`` (NOT ``'se chamava``).\n"
        "  - ``d'avoir dédié`` → ``por ter dedicado`` (NOT ``por "
        "'ter dedicado``).\n"
        "  - ``l'enfant qu'a été`` → ``a criança que foi`` (NOT "
        "``à 'criança que 'foi``)."
    ),
    "it": (
        "Italian uses apostrophe contractions for elision (``l'amico``, "
        "``dell'amore``, ``un'idea``). Carry the apostrophe over only "
        "when the target language uses the same convention; otherwise "
        "render the full target-language form."
    ),
    "de": (
        "German capitalizes every common noun. Do NOT preserve those "
        "capitals in the target unless the target language also "
        "capitalizes nouns; render proper-noun capitalization "
        "according to target-language rules."
    ),
    "es": (
        "Spanish opens questions and exclamations with ``¿`` / ``¡``. "
        "Most other languages only use the closing mark, so translate "
        "the punctuation to whatever the target language conventionally "
        "uses for questions and exclamations."
    ),
}
"""Source-language idiosyncrasies the model must NOT carry across.

Keyed by the BCP-47 primary subtag (``fr``, ``de``, …). Lookup falls
back from the full tag (``fr-CA``) to the primary subtag, so adding a
``fr-CA``-specific note is a one-key override rather than a
per-variant copy. Each value is a single short paragraph; the
prompt-builder wraps it in a bullet line.

The dict is intentionally narrow: it only holds patterns curators
have observed mistranslated in the wild (or that come up reliably
when reviewing competing translations). Adding a language pair is a
two-line PR that doesn't touch the universal hard-rules block.
"""


_TARGET_LANG_NOTES: dict[str, str] = {
    "pt": (
        "Portuguese does NOT use apostrophe contractions in modern "
        "prose: write ``eu tinha``, ``se chamava``, ``ele era`` as "
        "two separate words with a normal space between them. "
        "Preposition + article contractions use dedicated glyphs "
        "(``de + a = da``, ``em + o = no``, ``por + a = pela``), "
        "never an apostrophe. NEVER write an apostrophe directly "
        "in front of a Portuguese word — leading apostrophes "
        "(``'tenho``, ``'ser``, ``'criança``) are ALWAYS wrong, "
        "even when carrying the pattern over from a French / "
        "Italian / Catalan source that elides verbs with "
        "apostrophes. Dialogue is conventionally introduced with "
        "an em-dash (``— Olá!``). Quotation marks, when used, are "
        "guillemets ``«…»`` or curly doubles ``\u201c\u2026\u201d``."
    ),
    "en": (
        "English uses apostrophes for contractions (``don't``, "
        "``it's``) and possessives (``Mary's``). Quotation marks are "
        'typically straight ``"…"`` or curly ``"…"``; guillemets '
        "``«…»`` read as foreign and should be replaced unless the "
        "source-language flavor is deliberately preserved."
    ),
    "fr": (
        "French uses apostrophe contractions for elision "
        "(``j'avais``, ``l'arbre``). Quotation marks are conventionally "
        "guillemets ``« … »`` with thin spaces inside; double straight "
        "quotes are anglicisms in French prose."
    ),
    "es": (
        "Spanish opens questions and exclamations with ``¿`` / ``¡`` "
        "and closes them with ``?`` / ``!``. Dialogue is conventionally "
        "introduced with an em-dash (``— Hola``)."
    ),
    "it": (
        "Italian uses apostrophe contractions for elision (``l'amico``, "
        "``dell'amore``). Quotation marks are conventionally guillemets "
        "``« … »``; double straight quotes read as anglicisms."
    ),
}
"""Target-language conventions the model SHOULD follow.

Same shape and lookup rules as :data:`_SOURCE_LANG_NOTES`. The
companion side: while the source-side notes warn against carrying
patterns over, these notes prescribe the conventions the target
language actually uses, so the model has a positive instruction
rather than only a "don't do X" reminder.
"""


def _lookup_lang_note(lang: str, table: dict[str, str]) -> str | None:
    """Look up a language note, trying the full tag then the primary subtag.

    BCP-47 codes can carry region / script subtags (``pt-BR``,
    ``zh-Hant``); we fall back to the primary subtag (``pt``, ``zh``)
    so the common case is one entry per language. Add a regional
    override (``pt-br``) only when its conventions actually diverge
    from the family default.
    """

    if not lang:
        return None
    full = lang.strip().lower()
    direct = table.get(full)
    if direct is not None:
        return direct
    primary = full.split("-", 1)[0]
    if primary != full:
        return table.get(primary)
    return None


def _format_language_pair_notes(source_lang: str, target_lang: str) -> str:
    """Render a conditional language-pair tips block.

    Stays empty for the long tail of language pairs where we have no
    notes on file. When either side has notes (or both), they're
    rendered in a single block right above the style guide so the
    model sees pair-specific guidance close to the rest of the
    contextual context. Each line is a short bullet that complements
    rule 2/3's abstract "render typography per target conventions"
    instruction with a concrete cue for *this* run.

    The block uses the bare BCP-47 codes — the named-label rendering
    (``French (fr)``) belongs to the main "Translate from X to Y"
    sentence at the top of the prompt, where the model first picks up
    its working pair. Repeating the prose name in every bullet would
    just inflate the prompt without giving the model new information,
    and curators reading the rendered prompt have already been
    "anchored" by the named header.
    """

    src_note = _lookup_lang_note(source_lang, _SOURCE_LANG_NOTES)
    tgt_note = _lookup_lang_note(target_lang, _TARGET_LANG_NOTES)
    if not src_note and not tgt_note:
        return ""

    lines = [f"Language-pair notes ({source_lang} → {target_lang}):"]
    if src_note:
        lines.append(f"  - When translating FROM {source_lang}: {src_note}")
    if tgt_note:
        lines.append(f"  - When translating TO {target_lang}: {tgt_note}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_context_block(context: Sequence[ContextSegment]) -> str:
    """Render the preceding-segments block surfaced to the translator.

    Stays empty when ``context`` is empty (the common case for
    standalone reader translation, grouped batches, or when the user
    has not opted in via :class:`epublate.core.pipeline.ContextOptions`).
    Otherwise the block lists each preceding segment with its
    source / curator-approved target side-by-side, oldest first, so
    the LLM sees the recency gradient. We deliberately fence the block
    with a "translate ONLY the user's segment" reminder so the model
    doesn't try to retranslate the context.
    """

    if not context:
        return ""
    ordered = sorted(context, key=lambda c: c.segments_back, reverse=True)
    lines = [
        "Preceding segments (context only — DO NOT translate them; "
        "translate ONLY the user's segment below):",
    ]
    for entry in ordered:
        src = entry.source_text.strip() or "(empty)"
        tgt = (
            entry.target_text.strip()
            if entry.target_text and entry.target_text.strip()
            else "(not yet translated)"
        )
        lines.append(f"  - source: {src}")
        lines.append(f"    target: {tgt}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_translator_messages(
    *,
    source_lang: str,
    target_lang: str,
    source_text: str,
    style_guide: str | None = None,
    glossary: Sequence[GlossaryConstraint] = (),
    target_only_glossary: Sequence[TargetOnlyConstraint] = (),
    context: Sequence[ContextSegment] = (),
) -> list[Message]:
    """Construct the chat messages for one translator call (PRD §8.1).

    ``target_only_glossary`` carries entries whose canonical *target*
    spelling is pinned but whose *source* spelling is unknown
    (PRD §4.3 / F-LB-9). They render in a separate prompt block that
    asks the model to map source-language references it sees in the
    segment to the canonical target form. Validator-side these are
    soft-locked — a missed match is a warning, not a hard failure.

    ``context`` carries up to N preceding segments from the same
    chapter (oldest first in the rendered block). The pipeline picks
    them based on the user's :class:`epublate.core.pipeline.ContextOptions`
    knobs (``max_segments`` and ``max_chars``); a segment is *never*
    split to fit the char cap. The block tells the model these are
    context only — the user message still contains the single segment
    to translate.

    Language-pair labels are rendered as ``Name (code)`` (e.g.
    ``French (fr)`` → ``Brazilian Portuguese (pt-BR)``) so the LLM
    sees both the unambiguous BCP-47 tag and the human name. Pair-
    specific typography notes only render when
    :func:`_format_language_pair_notes` has guidance for the combo,
    keeping the prompt compact for the long tail.
    """

    if not source_text:
        raise ValueError("source_text must not be empty")

    style_guide_block = (
        f"Style guide:\n{style_guide.strip()}\n\n" if style_guide else ""
    )
    glossary_block = _format_glossary_block(glossary)
    target_only_block = _format_target_only_block(target_only_glossary)
    language_notes_block = _format_language_pair_notes(source_lang, target_lang)
    context_block = _format_context_block(context)

    system_content = _SYSTEM_PROMPT_TEMPLATE.format(
        source_lang=_format_language_label(source_lang),
        target_lang=_format_language_label(target_lang),
        style_guide_block=style_guide_block,
        glossary_block=glossary_block,
        target_only_block=target_only_block,
        language_notes_block=language_notes_block,
        context_block=context_block,
    )

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=source_text),
    ]


def _format_glossary_block(glossary: Sequence[GlossaryConstraint]) -> str:
    if not glossary:
        return "Glossary: (empty for this segment).\n\n"
    by_status: dict[GlossaryStatus, list[GlossaryConstraint]] = {
        "locked": [],
        "confirmed": [],
        "proposed": [],
    }
    for entry in glossary:
        by_status.setdefault(entry.status, []).append(entry)

    lines = ["Glossary:"]
    for status in ("locked", "confirmed", "proposed"):
        bucket = by_status.get(status, [])
        if not bucket:
            continue
        lines.append(f"  {status} entries (must use the canonical target term):")
        for entry in bucket:
            gender_marker = (
                f" (gender: {entry.gender})"
                if entry.gender and entry.gender != "unspecified"
                else ""
            )
            note = f" — {entry.notes}" if entry.notes else ""
            lines.append(
                f"    - [{entry.type}] {entry.source_term} → "
                f"{entry.target_term}{gender_marker}{note}"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_target_only_block(
    glossary: Sequence[TargetOnlyConstraint],
) -> str:
    """Render the target-only constraint block (PRD §4.3 / F-LB-9).

    The block deliberately lives separately from the source-keyed
    glossary section because the contract differs: there is no source
    pattern to match on, the model has to *infer* the binding from
    context. Returns an empty string when the list is empty so the
    prompt template stays compact for the common case.
    """

    if not glossary:
        return ""
    by_status: dict[GlossaryStatus, list[TargetOnlyConstraint]] = {
        "locked": [],
        "confirmed": [],
        "proposed": [],
    }
    for entry in glossary:
        by_status.setdefault(entry.status, []).append(entry)

    lines = [
        "Canonical target terms used in this work (no source spelling on file):",
    ]
    has_any = False
    for status in ("locked", "confirmed"):
        bucket = by_status.get(status, [])
        if not bucket:
            continue
        has_any = True
        lines.append(
            f"  {status} target forms (use the canonical spelling when applicable):"
        )
        for entry in bucket:
            aliases = (
                f"  (aliases: {', '.join(entry.target_aliases)})"
                if entry.target_aliases
                else ""
            )
            gender_marker = (
                f" (gender: {entry.gender})"
                if entry.gender and entry.gender != "unspecified"
                else ""
            )
            note = f" — {entry.notes}" if entry.notes else ""
            lines.append(
                f"    - [{entry.type}] {entry.target_term}"
                f"{gender_marker}{aliases}{note}"
            )
    if not has_any:
        return ""
    lines.append("")
    lines.append(
        "If you encounter a source-language term in this segment that names "
        "one of the entities above, you MUST translate it using the canonical "
        "target form. If no source term in this segment maps to one of these "
        "entities, ignore this list entirely."
    )
    lines.append("")
    return "\n".join(lines) + "\n"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_translator_response(content: str) -> TranslatorTrace:
    """Parse the translator's JSON response (PRD §8.1, F-LLM-3 fallback).

    Tries strict ``json.loads`` first. If that fails, attempts to recover
    the first ``{...}`` block in the payload — this catches the common
    failure mode where a permissive endpoint wraps JSON in prose despite
    the system prompt's instructions. Anything that still doesn't parse
    raises :class:`~epublate.errors.LLMResponseError`.
    """

    if not content or not content.strip():
        raise LLMResponseError("translator response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "translator response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from translator response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "translator response top-level must be a JSON object; "
            f"got {type(data).__name__}"
        )
    if "target" not in data:
        raise LLMResponseError("translator response missing required 'target' field")
    target = data.get("target")
    if not isinstance(target, str):
        raise LLMResponseError("translator response 'target' must be a string")

    used_entries_raw = data.get("used_entries", [])
    new_entities_raw = data.get("new_entities", [])
    notes = data.get("notes")

    if not isinstance(used_entries_raw, list):
        raise LLMResponseError("translator response 'used_entries' must be a list")
    used_entries = [str(item) for item in used_entries_raw]

    if not isinstance(new_entities_raw, list):
        raise LLMResponseError("translator response 'new_entities' must be a list")
    new_entities: list[dict[str, Any]] = []
    for item in new_entities_raw:
        if not isinstance(item, dict):
            raise LLMResponseError("each entry in 'new_entities' must be a JSON object")
        new_entities.append(dict(item))

    if notes is not None and not isinstance(notes, str):
        raise LLMResponseError("translator response 'notes' must be a string if set")

    return TranslatorTrace(
        target=target,
        used_entries=used_entries,
        new_entities=new_entities,
        notes=notes,
    )


def build_group_translator_messages(
    *,
    source_lang: str,
    target_lang: str,
    source_items: Sequence[tuple[int, str]],
    style_guide: str | None = None,
    glossary: Sequence[GlossaryConstraint] = (),
    target_only_glossary: Sequence[TargetOnlyConstraint] = (),
) -> list[Message]:
    """Construct chat messages for a *grouped* translator call.

    ``source_items`` is a sequence of ``(id, source_text)`` tuples. The
    ids are echoed back by the model so the pipeline can map translations
    to the originating :class:`~epublate.db.repo.SegmentRow`-s without
    relying on positional alignment (the model occasionally re-orders).
    The caller is expected to filter segments down to the
    grouping-eligible subset (short, placeholder-free) before calling
    this — see ``epublate.core.pipeline.translate_segments_grouped``.

    ``target_only_glossary`` mirrors :func:`build_translator_messages` —
    target-only constraints (PRD §4.3 / F-LB-9) get rendered in their
    own block when present.
    """

    if not source_items:
        raise ValueError("source_items must not be empty")
    seen: set[int] = set()
    for item_id, text in source_items:
        if item_id in seen:
            raise ValueError(f"duplicate item id {item_id} in source_items")
        if not text:
            raise ValueError(f"source_items[{item_id}] has empty source_text")
        seen.add(item_id)

    style_guide_block = (
        f"Style guide:\n{style_guide.strip()}\n\n" if style_guide else ""
    )
    glossary_block = _format_glossary_block(glossary)
    target_only_block = _format_target_only_block(target_only_glossary)
    language_notes_block = _format_language_pair_notes(source_lang, target_lang)

    system_content = _GROUP_SYSTEM_PROMPT_TEMPLATE.format(
        source_lang=_format_language_label(source_lang),
        target_lang=_format_language_label(target_lang),
        style_guide_block=style_guide_block,
        glossary_block=glossary_block,
        target_only_block=target_only_block,
        language_notes_block=language_notes_block,
    )

    user_payload = {
        "items": [{"id": item_id, "source": text} for item_id, text in source_items]
    }
    user_content = json.dumps(user_payload, ensure_ascii=False)

    return [
        Message(role="system", content=system_content),
        Message(role="user", content=user_content),
    ]


def parse_group_translator_response(
    content: str,
    *,
    expected_ids: Sequence[int] | None = None,
) -> GroupTranslatorTrace:
    """Parse a grouped translator response.

    Mirror of :func:`parse_translator_response`. Raises
    :class:`~epublate.errors.LLMResponseError` if the payload isn't
    valid JSON, doesn't have the expected shape, or — when
    ``expected_ids`` is provided — omits any of the expected ids.
    Extra ids are tolerated but silently dropped by downstream callers.
    """

    if not content or not content.strip():
        raise LLMResponseError("group translator response was empty")

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(content)
        if match is None:
            raise LLMResponseError(
                "group translator response is not JSON and contains no JSON object"
            ) from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"failed to recover JSON from group translator response: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError(
            "group translator response top-level must be a JSON object"
        )
    translations_raw = data.get("translations")
    if not isinstance(translations_raw, list):
        raise LLMResponseError("group translator response missing 'translations' list")

    items: list[GroupTranslatorItem] = []
    seen_ids: set[int] = set()
    for raw in translations_raw:
        if not isinstance(raw, dict):
            raise LLMResponseError("each 'translations' entry must be a JSON object")
        try:
            item = GroupTranslatorItem.model_validate(raw)
        except Exception as exc:  # pydantic surface
            raise LLMResponseError(f"invalid 'translations' entry: {exc}") from exc
        if item.id in seen_ids:
            raise LLMResponseError(
                f"duplicate item id {item.id} in group translator response"
            )
        seen_ids.add(item.id)
        items.append(item)

    if expected_ids is not None:
        missing = [i for i in expected_ids if i not in seen_ids]
        if missing:
            raise LLMResponseError(
                f"group translator response missing ids: {missing!r}"
            )

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise LLMResponseError("group translator response 'notes' must be a string")

    return GroupTranslatorTrace(translations=items, notes=notes)


__all__ = [
    "ContextSegment",
    "GlossaryConstraint",
    "GroupTranslatorItem",
    "GroupTranslatorTrace",
    "TargetOnlyConstraint",
    "TranslatorTrace",
    "build_group_translator_messages",
    "build_translator_messages",
    "parse_group_translator_response",
    "parse_translator_response",
]
