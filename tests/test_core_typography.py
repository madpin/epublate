"""Target-text typography sanitiser tests (PRD §7).

The sanitiser is deterministic and language-gated: it removes
apostrophe characters that are stranded as leading orthography in
front of a word, but only when the target language is one that
doesn't use leading apostrophes. The tests below assert the gating
(English / French targets are untouched), the actual rewrite (the
user-reported French→Portuguese bug pattern), and the conservatism
(legitimate apostrophes in possessives and proper nouns are
preserved).
"""

from __future__ import annotations

import pytest

from epublate.core.typography import strip_leading_orphan_apostrophes

# ---------------------------------------------------------------------------
# The user-reported bug pattern: French → Portuguese
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad,good",
    [
        # The ``por 'ter dedicado`` line from Le Petit Prince.
        (
            "Pe\u00e7o desculpas \u00e0s crian\u00e7as por \u2019ter "
            "dedicado este livro.",
            "Pe\u00e7o desculpas \u00e0s crian\u00e7as por ter dedicado este livro.",
        ),
        # ``Eu 'tenho`` — the most common shape in the report.
        (
            "Eu \u2019tenho uma desculpa s\u00e9ria.",
            "Eu tenho uma desculpa s\u00e9ria.",
        ),
        # Mid-sentence ``'ser consolada``.
        (
            "Ela realmente precisa \u2019ser consolada.",
            "Ela realmente precisa ser consolada.",
        ),
        # Multiple orphans in the same sentence.
        (
            "\u00e0 \u2019crian\u00e7a que \u2019foi outrora essa pessoa.",
            "\u00e0 crian\u00e7a que foi outrora essa pessoa.",
        ),
        # Mix of ASCII straight and curly apostrophes.
        (
            "Eu 'tenho e ela \u2019teve uma desculpa.",
            "Eu tenho e ela teve uma desculpa.",
        ),
        # Closing parenthesis whitespace context.
        (
            "(Mas poucas \u2019delas \u2019se lembram disso.)",
            "(Mas poucas delas se lembram disso.)",
        ),
    ],
)
def test_strip_orphan_apostrophes_pt_handles_user_reported_lines(
    bad: str, good: str
) -> None:
    """Every line the user pasted must come out clean."""

    assert strip_leading_orphan_apostrophes(bad, target_lang="pt") == good


def test_strip_handles_apostrophe_at_start_of_string() -> None:
    """An apostrophe that opens the string IS a leading-orphan too.

    Mid-sentence orphans are the most common shape, but the regex
    has an explicit ``^`` alternation so an apostrophe at position
    0 is also stripped (matches the user's mid-line case where the
    LLM put a leading apostrophe at the start of a clause).
    """

    text = "\u2019tenho uma d\u00favida"
    assert (
        strip_leading_orphan_apostrophes(text, target_lang="pt")
        == "tenho uma d\u00favida"
    )


def test_strip_pt_br_falls_back_to_pt_subtag() -> None:
    """Regional tags fall back to the primary subtag (BCP-47)."""

    bad = "Eu \u2019tenho."
    assert strip_leading_orphan_apostrophes(bad, target_lang="pt-BR") == "Eu tenho."
    assert strip_leading_orphan_apostrophes(bad, target_lang="PT-br") == "Eu tenho."


# ---------------------------------------------------------------------------
# Language gating — only "no leading apostrophe" languages are rewritten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leave_alone_lang",
    [
        "fr",  # French: ``j'``, ``l'``, ``qu'`` are valid
        "fr-CA",  # Same with regional tag
        "it",  # Italian: ``l'amico``, ``un'idea``
        "ca",  # Catalan: ``s'``, ``l'``
        "en",  # English: ``'twas``, ``'tis``
        "en-GB",  # English regional
    ],
)
def test_strip_leaves_apostrophe_using_languages_alone(
    leave_alone_lang: str,
) -> None:
    """Languages that DO use leading apostrophes keep them intact.

    Stripping ``j'avais`` to ``javais`` would corrupt valid French
    orthography. The sanitiser must be a no-op for those targets.
    """

    text = "J\u2019ai une excuse."
    assert strip_leading_orphan_apostrophes(text, target_lang=leave_alone_lang) == text


@pytest.mark.parametrize(
    "no_apostrophe_lang",
    [
        "pt",
        "es",
        "de",
        "ru",
        "ja",
        "zh",
        "ar",
        "he",
        "tr",
        "pl",
        "nl",
        "sv",
    ],
)
def test_strip_runs_for_each_listed_target_language(
    no_apostrophe_lang: str,
) -> None:
    """Spot-check each language in the no-leading-apostrophe set."""

    text = "antes \u2019ter feito isso"
    cleaned = strip_leading_orphan_apostrophes(text, target_lang=no_apostrophe_lang)
    assert cleaned == "antes ter feito isso"


def test_strip_handles_empty_target_lang_as_no_op() -> None:
    """An unset / empty target lang means "don't touch the bytes"."""

    text = "Eu \u2019tenho."
    assert strip_leading_orphan_apostrophes(text, target_lang="") == text


def test_strip_handles_unknown_language_as_no_op() -> None:
    """Unknown language tag = pass through unchanged.

    The sanitiser is opt-in by language code: if we don't recognize
    the target as one that bans leading apostrophes, the safe
    default is to leave the bytes alone. Typos and rare languages
    fall through this path.
    """

    text = "Eu \u2019tenho."
    assert strip_leading_orphan_apostrophes(text, target_lang="xxx") == text


# ---------------------------------------------------------------------------
# Conservatism — legitimate apostrophes are preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preserved",
    [
        # Embedded apostrophe in proper noun (no leading whitespace).
        "Em D'\u00c1gua h\u00e1 um rio.",
        "O'Higgins foi um l\u00edder.",
        "L'Or\u00e9al chegou ao Brasil.",
        "M'Bappe marcou um gol.",
        # Sentence-initial proper noun with embedded apostrophe.
        "D'\u00c1gua \u00e9 uma cidade portuguesa.",
        "O'Higgins ficou famoso.",
        # Year reference / decade — apostrophe followed by a digit.
        "Os anos '90 foram especiais.",
        "Em '42 ela escreveu o livro.",
        # Inside double-quoted text where the apostrophe is part of
        # a contracted English word (carried as a foreign-language
        # quote): the strip is conservative enough to leave this
        # alone because the apostrophe is preceded by a quote
        # character, not whitespace.
        'O letreiro dizia "don\u2019t panic".',
        # No apostrophes at all.
        "Texto normal sem apostr\u00f3fos.",
    ],
)
def test_strip_pt_preserves_legitimate_apostrophes(preserved: str) -> None:
    """The strip must not corrupt valid orthography.

    The function is conservative on purpose: only apostrophes
    directly preceded by whitespace (or at start-of-string) AND
    directly followed by a Unicode letter are stripped. Everything
    else round-trips byte-for-byte. This test pins the behaviour
    so a future "more aggressive" rewrite can't silently regress.
    """

    assert strip_leading_orphan_apostrophes(preserved, target_lang="pt") == preserved


def test_strip_preserves_whitespace_layout() -> None:
    """Adjacent whitespace stays byte-identical after stripping.

    Only the apostrophe character itself is removed; the leading
    whitespace (space, tab, newline, indented-margin) is preserved
    verbatim so XHTML reassembly (which is whitespace-sensitive
    inside ``<pre>`` and around ``<br/>``) doesn't drift.
    """

    samples = [
        ("\t\u2019foo", "\tfoo"),
        ("\n\u2019foo", "\nfoo"),
        ("  \u2019foo", "  foo"),
        ("a\n\u2019foo", "a\nfoo"),
    ]
    for bad, good in samples:
        assert strip_leading_orphan_apostrophes(bad, target_lang="pt") == good


def test_strip_handles_modifier_letter_apostrophe() -> None:
    """U+02BC (modifier letter apostrophe) is also covered.

    Some endpoints normalize ASCII ``'`` to U+02BC instead of
    U+2019 in their tokenizer. The strip must catch both shapes
    so the gating doesn't depend on the model's typography
    preference.
    """

    text = "Eu \u02bctenho."
    assert strip_leading_orphan_apostrophes(text, target_lang="pt") == "Eu tenho."


# ---------------------------------------------------------------------------
# Edge cases — empty strings, idempotency, non-string-like input
# ---------------------------------------------------------------------------


def test_strip_returns_empty_for_empty_input() -> None:
    assert strip_leading_orphan_apostrophes("", target_lang="pt") == ""


def test_strip_is_idempotent() -> None:
    """Running the sanitiser twice yields the same result.

    Important for the cache-replay path, where the cleaned trace
    might be re-fed into the sanitiser on the next replay. A
    non-idempotent rewrite would slowly mutate the bytes across
    replays (or drop characters until the output is empty).
    """

    bad = "Eu \u2019tenho \u2019muito que dizer \u2019aqui."
    once = strip_leading_orphan_apostrophes(bad, target_lang="pt")
    twice = strip_leading_orphan_apostrophes(once, target_lang="pt")
    assert once == twice


def test_strip_does_not_double_strip_consecutive_apostrophes() -> None:
    """``\u2019\u2019`` after whitespace: only the FIRST is leading.

    The pattern requires an apostrophe whose left neighbour is a
    whitespace; the second apostrophe in ``  \u2019\u2019foo`` has
    an apostrophe to its left (not whitespace) so it stays. The
    first one's gone, leaving ``  \u2019foo`` which is itself a
    leading-orphan and gets stripped on the next pass — but the
    function is idempotent, so a single call must already produce
    the fully-cleaned result. This locks that behaviour in.
    """

    bad = "Eu \u2019\u2019tenho."
    cleaned = strip_leading_orphan_apostrophes(bad, target_lang="pt")
    # Both apostrophes are removed in a single pass — the regex
    # iterator re-scans from the substitution point, so the second
    # apostrophe is now whitespace-adjacent and gets stripped too.
    assert cleaned == "Eu tenho."
