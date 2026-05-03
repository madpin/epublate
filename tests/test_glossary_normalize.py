"""Tests for the glossary particle / lemma normalizer (PRD F-LB-3)."""

from __future__ import annotations

import pytest

from epublate.glossary.normalize import (
    analyze_pair,
    find_doubled_particles,
    leading_particle,
    normalize_term,
)


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        # English: only the/a/an
        ("the USA", "en", "the"),
        ("The USA", "en", "the"),
        ("USA", "en", None),
        ("a Knight", "en", "a"),
        ("Knight", "en", None),
        ("an apple", "en", "an"),
        # Portuguese: definite article + contractions are the
        # interesting cases.
        ("o Senado", "pt", "o"),
        ("a Câmara", "pt", "a"),
        ("os Estados Unidos", "pt", "os"),
        ("na Europa", "pt", "na"),
        ("do Brasil", "pt", "do"),
        ("Câmara", "pt", None),
        ("Europa", "pt", None),
        # Punctuation around the head shouldn't fool us.
        ('"the USA"', "en", "the"),
        # Empty / single-word inputs are not "particles".
        ("the", "en", None),
        ("", "en", None),
        # BCP-47 region codes collapse to the primary subtag.
        ("the USA", "en-GB", "the"),
        ("o Senado", "pt-BR", "o"),
        # Unknown language falls back to English (no PT particles).
        ("o Senado", "ja", None),
        # Default lang (None) matches the ``en`` set.
        ("the USA", None, "the"),
    ],
)
def test_leading_particle(text: str, lang: str | None, expected: str | None) -> None:
    assert leading_particle(text, lang=lang) == expected


@pytest.mark.parametrize(
    ("text", "lang", "stripped", "particle"),
    [
        ("the USA", "en", "USA", "the"),
        ("The Senate", "en", "Senate", "the"),
        ("USA", "en", "USA", None),
        ("na Europa", "pt", "Europa", "na"),
        ("Europa", "pt", "Europa", None),
        # ``the`` alone never strips down to "" — a bare particle
        # entry is almost certainly garbage and we leave it for the
        # symmetry check to flag.
        ("the", "en", "the", None),
        ("  the  USA  ", "en", "USA", "the"),
    ],
)
def test_normalize_term(
    text: str, lang: str, stripped: str, particle: str | None
) -> None:
    result = normalize_term(text, lang=lang)
    assert result.stripped == stripped
    assert result.particle == particle


def test_analyze_pair_lemma_form_is_symmetric() -> None:
    sym = analyze_pair(
        source_term="USA",
        target_term="EUA",
        source_lang="en",
        target_lang="pt",
    )
    assert sym.symmetric is True
    assert sym.source_particle is None
    assert sym.target_particle is None


def test_analyze_pair_balanced_articles_is_symmetric() -> None:
    sym = analyze_pair(
        source_term="the USA",
        target_term="os EUA",
        source_lang="en",
        target_lang="pt",
    )
    assert sym.symmetric is True
    assert sym.source_particle == "the"
    assert sym.target_particle == "os"


def test_analyze_pair_target_only_article_rejected() -> None:
    """The ``Europe → na Europa`` shape that the user reported."""

    sym = analyze_pair(
        source_term="Europe",
        target_term="na Europa",
        source_lang="en",
        target_lang="pt",
    )
    assert sym.symmetric is False
    assert sym.source_particle is None
    assert sym.target_particle == "na"
    assert "na" in sym.message
    assert "Europe" in sym.message


def test_analyze_pair_source_only_article_rejected() -> None:
    """The mirror case: ``the USA → EUA``."""

    sym = analyze_pair(
        source_term="the USA",
        target_term="EUA",
        source_lang="en",
        target_lang="pt",
    )
    assert sym.symmetric is False
    assert sym.source_particle == "the"
    assert sym.target_particle is None
    assert "the" in sym.message


def test_analyze_pair_target_only_entry_is_trivially_symmetric() -> None:
    """Target-only entries (F-LB-9) have no source side to balance.

    A Lore Book authored from a translated edition pins ``"Câmara"``
    without ever recording the source spelling. The validator at
    translation time still catches doubled particles in the rendered
    output.
    """

    sym = analyze_pair(
        source_term=None,
        target_term="na Câmara",
        source_lang=None,
        target_lang="pt",
    )
    assert sym.symmetric is True


@pytest.mark.parametrize(
    ("text", "lang", "expected"),
    [
        # Reports the char offset of the FIRST particle in the pair.
        ("Na na Europa", "pt", [("na", 0)]),
        ("Estava no no Brasil ontem.", "pt", [("no", 7)]),
        ("the the Senate voted", "en", [("the", 0)]),
        # Sentence-boundary "in. In any case" should NOT match because
        # tokens aren't word-adjacent (punctuation between them).
        ("Stay in. In any case, leave.", "en", []),
        # Adjacent-but-not-particle words are ignored — the validator
        # only cares about function words.
        ("the dog the cat", "en", []),
        # Multiple hits in one sentence are all reported.
        ("na na Europa e na na França", "pt", [("na", 0), ("na", 15)]),
    ],
)
def test_find_doubled_particles_catches_user_reported_shape(
    text: str, lang: str, expected: list[tuple[str, int]]
) -> None:
    assert find_doubled_particles(text, lang=lang) == expected


def test_find_doubled_particles_extra_particles_param() -> None:
    """``extra_particles`` lets callers extend the policed set."""

    text = "I'm so so happy."
    base = find_doubled_particles(text, lang="en")
    assert base == []  # ``so`` isn't a default function word
    extended = find_doubled_particles(text, lang="en", extra_particles=("so",))
    assert extended == [("so", 4)]
