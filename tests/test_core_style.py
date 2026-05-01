"""Style profile registry tests (PRD F-STYLE-1)."""

from __future__ import annotations

import pytest

from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    get_profile,
    label_for,
    list_profiles,
    profile_choices,
    resolve_style_guide,
    suggest_style_profile,
)


def test_default_profile_is_registered() -> None:
    assert DEFAULT_STYLE_PROFILE in PROFILE_REGISTRY


def test_every_profile_has_non_empty_prompt_block() -> None:
    for prof in list_profiles():
        assert prof.id
        assert prof.name
        assert prof.description
        # Prompt block needs to be substantive enough to actually steer
        # the LLM — a one-liner is too weak.
        assert len(prof.prompt_block) >= 80, f"{prof.id} prompt block too short"


def test_get_profile_returns_none_for_unknown() -> None:
    assert get_profile("does-not-exist") is None
    assert get_profile(DEFAULT_STYLE_PROFILE) is not None


def test_resolve_style_guide_uses_preset_when_no_custom_text() -> None:
    resolved = resolve_style_guide(profile_id="children_picture")
    assert resolved is not None
    assert "young readers" in resolved.lower() or "children" in resolved.lower()


def test_resolve_style_guide_custom_text_wins_over_preset() -> None:
    resolved = resolve_style_guide(
        profile_id="literary_fiction",
        custom_text="my own paragraph about voice",
    )
    assert resolved == "my own paragraph about voice"


def test_resolve_style_guide_empty_custom_text_falls_back_to_preset() -> None:
    resolved = resolve_style_guide(profile_id="literary_fiction", custom_text="   ")
    assert resolved is not None
    # Must be the preset's text, not the (effectively empty) custom string.
    expected = PROFILE_REGISTRY["literary_fiction"].prompt_block
    assert resolved == expected


def test_resolve_style_guide_returns_none_when_nothing_set() -> None:
    assert resolve_style_guide(profile_id=None) is None
    assert resolve_style_guide(profile_id=None, custom_text=None) is None
    assert resolve_style_guide(profile_id=None, custom_text="") is None


def test_resolve_style_guide_unknown_profile_falls_back_to_none() -> None:
    # Unknown id is tolerated (post-rename / future profile compat) and
    # produces no style block — better than crashing on read.
    assert resolve_style_guide(profile_id="future-preset") is None


@pytest.mark.parametrize(
    "register, audience, expected",
    [
        (None, "children", "children_picture"),
        (None, "kids", "children_picture"),
        ("neutral", "middle_grade", "middle_grade"),
        ("casual", "young_adult", "young_adult"),
        ("casual", "YA", "young_adult"),
        ("explicit", "adult", "explicit_adult"),
        ("erotic", None, "explicit_adult"),
        ("technical", None, "technical_manual"),
        ("how-to", None, "technical_manual"),
        ("academic", "general", "academic"),
        ("journalistic", None, "journalistic"),
        ("romance", None, "cozy_romance"),
        ("thriller", "adult", "genre_fiction"),
        ("literary", "adult", "literary_fiction"),
        # Expanded catalog mappings (PRD F-STYLE-1 — extended).
        ("historical", "adult", "historical_fiction"),
        ("period", None, "historical_fiction"),
        ("classic", "adult", "classic_literature"),
        ("Victorian", None, "classic_literature"),
        ("19th century", None, "classic_literature"),
        ("memoir", None, "memoir_biography"),
        ("biography", None, "memoir_biography"),
        ("autobiographical", "adult", "memoir_biography"),
        ("noir", "adult", "noir_crime"),
        ("hard-boiled", None, "noir_crime"),
        ("horror", "adult", "horror_gothic"),
        ("Gothic", None, "horror_gothic"),
        ("humor", "adult", "humor_comedy"),
        ("comedic", None, "humor_comedy"),
        ("satirical", None, "humor_comedy"),
        ("poetic", None, "poetry_verse"),
        ("verse", None, "poetry_verse"),
        ("religious", None, "religious_spiritual"),
        ("liturgical", None, "religious_spiritual"),
        ("fairytale", None, "fairytale_folklore"),
        ("fairy tale", None, "fairytale_folklore"),
        ("mythology", None, "fairytale_folklore"),
        ("folktale", None, "fairytale_folklore"),
        (None, None, None),
        ("???", "???", None),
    ],
)
def test_suggest_style_profile_mapping(
    register: str | None, audience: str | None, expected: str | None
) -> None:
    assert suggest_style_profile(register=register, audience=audience) == expected


def test_label_for_handles_custom_and_unknown() -> None:
    assert label_for(None) == "Custom"
    assert label_for("literary_fiction") == "Literary fiction"
    assert "Unknown" in label_for("does-not-exist")


def test_profile_choices_pairs_name_with_id() -> None:
    pairs = profile_choices()
    assert len(pairs) == len(PROFILE_REGISTRY)
    names = {name for name, _ in pairs}
    ids = {pid for _, pid in pairs}
    assert ids == set(PROFILE_REGISTRY.keys())
    # Each name is unique so a Select widget can render them straight.
    assert len(names) == len(pairs)


_EXPECTED_PROFILE_IDS: frozenset[str] = frozenset(
    {
        "literary_fiction",
        "classic_literature",
        "historical_fiction",
        "children_picture",
        "middle_grade",
        "young_adult",
        "fairytale_folklore",
        "genre_fiction",
        "noir_crime",
        "horror_gothic",
        "cozy_romance",
        "explicit_adult",
        "humor_comedy",
        "memoir_biography",
        "poetry_verse",
        "religious_spiritual",
        "technical_manual",
        "academic",
        "journalistic",
    }
)


def test_shipped_catalog_covers_expected_tones() -> None:
    """Pin the catalog so an accidental deletion is caught.

    New additions should extend this set, not rename / drop members —
    ``project.style_profile`` rows persist these slugs, so removing one
    breaks old projects (cf. ``get_profile`` returning ``None`` for
    unknown ids).
    """

    assert set(PROFILE_REGISTRY.keys()) >= _EXPECTED_PROFILE_IDS
