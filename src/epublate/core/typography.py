"""Target-text typography sanitiser (PRD §7 / format-handling).

The translator prompt asks the model to render punctuation,
orthography, and contractions according to target-language
conventions. In practice, OpenAI-compatible endpoints occasionally
carry source-language patterns over verbatim — most often a French /
Italian / Catalan elision apostrophe (``d'avoir``, ``j'ai``,
``l'arbre``, ``qu'il``) gets stranded as a leading apostrophe in
front of a word the target language does not contract:

    French:      "J'ai une excuse." / "il a besoin d'être consolé."
    Portuguese:  "Eu 'tenho uma desculpa." / "ele precisa 'ser consolado."

Modern Portuguese, Spanish, German, Russian, and several other
languages never elide verbs that way. The orphaned apostrophe is
purely an orthographic artefact and the right fix is to drop it.

This module is the deterministic safety-net half of the fix:
:func:`strip_leading_orphan_apostrophes` rewrites the target text
so any apostrophe-character that's directly preceded by whitespace
(or sits at the start of the string) and directly followed by a
letter is removed. Languages where leading apostrophes are *valid*
orthography (English's ``'twas`` / ``'tis``, French's ``j'``,
Italian's ``l'``, Catalan's ``s'``) are left untouched — the gate
is the target-language whitelist
:data:`_TARGET_LANGS_WITHOUT_LEADING_APOSTROPHE`.

The companion prompt-side guidance lives in
:mod:`epublate.llm.prompts.translator` (see ``_TARGET_LANG_NOTES``),
which gives the model a worked example so the failure rate drops at
the source. Per the project conventions, the parser-side / pipeline-
side fix is the load-bearing one and the prompt rule is a hint.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["strip_leading_orphan_apostrophes"]


# Apostrophe-shaped characters the LLM may emit. Covers ASCII straight
# (U+0027), left/right single quotation marks (U+2018 / U+2019, the
# typographer's "smart" pair), and the modifier letter apostrophe
# (U+02BC, common in transliterations). Backtick (U+0060) is not
# included on purpose — it never reads as an apostrophe in prose
# and stripping it here would corrupt code-style spans.
_APOSTROPHE_CHARS = "'\u2018\u2019\u02bc"

# Pattern: a run of one or more apostrophe-shaped characters
# directly preceded by whitespace (or at the very start of the
# string) and directly followed by a non-space character. We test
# "is a letter" downstream via :func:`unicodedata.category` so the
# pattern itself stays simple and the letter check uses the full
# Unicode database (covers accented Latin, Cyrillic, CJK
# ideographs, Arabic, Hebrew, etc.). The greedy ``+`` on the
# apostrophe class collapses runs like ``  \u2019\u2019foo`` into
# a single match, so the rewrite is idempotent in one pass instead
# of needing a fixed-point loop. The lookbehind is variable-length
# so we use an alternation instead: ``(^|\s)`` matches
# start-of-string OR a whitespace character.
_LEADING_APOSTROPHE_RE = re.compile(
    rf"(^|\s)([{_APOSTROPHE_CHARS}]+)(?=\S)",
    flags=re.UNICODE,
)


# Target languages where a leading apostrophe in modern prose is
# essentially always a typo / model artefact. Keyed by BCP-47 primary
# subtag; the lookup falls back from the full tag (``pt-BR``) to the
# primary (``pt``) so adding a regional override is a one-key change.
#
# Languages NOT in this set (English: ``'twas``, French: ``j'``,
# Italian: ``l'``, Catalan: ``s'``) keep their apostrophes intact —
# the strip is gated on the target-language code at the call site.
#
# The set is intentionally narrow: it lists only families where the
# author has confirmed (a) leading apostrophes are not orthographic
# and (b) there's no widespread loanword convention that would
# false-positive. Adding a language is a one-line PR with a citation
# in the commit message.
_TARGET_LANGS_WITHOUT_LEADING_APOSTROPHE: frozenset[str] = frozenset(
    {
        "pt",  # Portuguese (BR + PT) — no verbal elision in prose.
        "es",  # Spanish — no verbal elision; ``'90s`` is rare.
        "de",  # German — apostrophe is a possessive, never leading.
        "ru",  # Russian (Cyrillic) — no leading apostrophe convention.
        "uk",  # Ukrainian.
        "ja",  # Japanese — no apostrophe orthography.
        "ko",  # Korean.
        "zh",  # Chinese (any variant).
        "ar",  # Arabic.
        "he",  # Hebrew.
        "tr",  # Turkish.
        "pl",  # Polish.
        "cs",  # Czech.
        "sk",  # Slovak.
        "hu",  # Hungarian.
        "ro",  # Romanian.
        "bg",  # Bulgarian.
        "hr",  # Croatian.
        "sr",  # Serbian.
        "sl",  # Slovenian.
        "fi",  # Finnish.
        "et",  # Estonian.
        "lv",  # Latvian.
        "lt",  # Lithuanian.
        "el",  # Greek.
        "th",  # Thai.
        "vi",  # Vietnamese.
        "id",  # Indonesian.
        "ms",  # Malay.
        "hi",  # Hindi.
        "fa",  # Persian.
        "nl",  # Dutch — possessive ``'s`` exists but never leading.
        "sv",  # Swedish.
        "no",  # Norwegian.
        "nb",  # Norwegian Bokmål.
        "nn",  # Norwegian Nynorsk.
        "da",  # Danish.
        "is",  # Icelandic.
    }
)


def _is_letter(ch: str) -> bool:
    """True if ``ch`` is a letter according to the Unicode database.

    Used to decide whether the character following a candidate
    apostrophe is "a word starter" (so the apostrophe is leading
    orthography we want to strip) versus a digit, punctuation, or
    quote (where the apostrophe is part of a year reference like
    ``'90s`` or a quote like ``"'tis"`` and we'd false-positive).
    """

    if not ch:
        return False
    return unicodedata.category(ch).startswith("L")


def strip_leading_orphan_apostrophes(text: str, *, target_lang: str | None) -> str:
    """Remove apostrophes stranded as leading characters in front of words.

    ``target_lang`` gates the rewrite: when it falls outside
    :data:`_TARGET_LANGS_WITHOUT_LEADING_APOSTROPHE` (or is ``None``
    / empty), the input is returned unchanged so French / Italian /
    English targets keep their valid leading apostrophes.

    The actual rewrite is conservative on purpose:

    * the apostrophe must be directly preceded by whitespace or sit
      at the start of the string (so embedded contractions like
      ``O'Higgins`` and Brazilian-style ``D'Água`` are kept);
    * the apostrophe must be directly followed by a Unicode letter
      (so digits and punctuation, e.g. ``'90s``, ``"'tis"``, are
      kept);
    * adjacent whitespace is preserved verbatim — only the
      apostrophe character is removed, so spacing and indentation
      around the rewrite are byte-identical.

    Returns the cleaned text. Pure function; no side effects.
    """

    if not text:
        return text
    if not target_lang:
        return text
    primary = target_lang.strip().lower().split("-", 1)[0]
    if primary not in _TARGET_LANGS_WITHOUT_LEADING_APOSTROPHE:
        return text

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)  # "" (start) or single whitespace char
        apostrophe_idx = match.end(2)
        # ``re.UNICODE`` makes ``\S`` match any non-space character,
        # but we still need a letter — not a digit / punctuation —
        # to count this as a leading-orphan apostrophe.
        if apostrophe_idx >= len(text):
            return match.group(0)
        next_char = text[apostrophe_idx]
        if not _is_letter(next_char):
            return match.group(0)
        return prefix

    return _LEADING_APOSTROPHE_RE.sub(_replace, text)
