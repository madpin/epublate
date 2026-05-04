"""Translator prompt builder + parser tests (PRD §8.1, F-LLM-3)."""

from __future__ import annotations

import pytest

from epublate.errors import LLMResponseError
from epublate.llm.prompts.translator import (
    ContextSegment,
    GlossaryConstraint,
    TargetOnlyConstraint,
    build_group_translator_messages,
    build_translator_messages,
    parse_translator_response,
)


def test_build_messages_has_system_and_user() -> None:
    messages = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="The [[T0]]old[[/T0]] man.",
    )
    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[1].role == "user"
    assert messages[1].content == "The [[T0]]old[[/T0]] man."


def test_system_prompt_lists_languages_and_placeholder_rules() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt-BR",
        source_text="hello",
    )
    assert "en" in system.content
    assert "pt-BR" in system.content
    assert "[[T0]]" in system.content
    assert "JSON" in system.content


def test_translator_prompt_carries_translate_everything_rule() -> None:
    """Real-world ePubs embed source-language quotations / book titles
    inside paragraphs and the model habitually leaves them as-is. The
    rule that says "translate every textual passage end-to-end" must
    travel in the prompt verbatim so we can audit for prompt drift the
    same way we audit the article-symmetry rule.
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="hello",
    )
    # Newlines in the rendered prompt make literal multi-word matches
    # brittle, so collapse whitespace for these assertions.
    body = " ".join(system.content.split())
    assert "Translate every textual passage end-to-end" in body
    # The actionable bits the model needs to *not* skip.
    assert "Embedded quotations" in body
    assert "footnote text" in body
    assert "book / article titles" in body


def test_translator_prompt_carries_whitespace_preservation_rule() -> None:
    """The LLM strips leading / trailing whitespace from chat responses
    by default, so the prompt has to spell out that the segment's outer
    whitespace is part of the source's identity (the reassembler also
    backstops this defensively).
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="hello",
    )
    body = " ".join(system.content.split())
    assert "Preserve the leading and trailing whitespace" in body
    assert "Do not strip" in body


def test_translator_prompt_carries_target_typography_rule() -> None:
    """The abstract typography rule travels in every prompt.

    The concrete language-pair examples live in the conditional
    ``Language-pair notes`` block (see
    :func:`test_language_pair_notes_block_renders_for_fr_pt`); the
    universal rule itself only carries the abstract instruction so
    the prompt stays compact for pairs we don't have notes for.
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="ja",
        source_text="hello",
    )
    body = " ".join(system.content.split())
    assert "target-language conventions" in body
    assert "language-specific" in body
    # The "see Language-pair notes below" handoff is the bridge
    # between the universal rule and the conditional block — keep
    # the wording stable so prompt drift is auditable.
    assert "Language-pair notes" in body


def test_language_pair_notes_block_renders_for_fr_pt() -> None:
    """fr → pt has notes on both sides; the block must surface them.

    Regression test for the curator-reported pattern where the model
    transcribed French apostrophe contractions into Portuguese
    (``j'avais`` → ``eu'tinha`` instead of ``eu tinha``). The
    language-pair notes block carries the concrete French and
    Portuguese typography reminders so the model sees pair-specific
    guidance close to the rest of the contextual context.
    """

    [system, _] = build_translator_messages(
        source_lang="fr",
        target_lang="pt-BR",
        source_text="Lorsque j'avais six ans.",
    )
    body = " ".join(system.content.split())
    assert "Language-pair notes (fr → pt-BR):" in body
    # FROM-section: French apostrophe contractions are the curator's
    # actual reported case — keep the ``j'avais`` cue.
    assert "When translating FROM fr" in body
    assert "j'avais" in body
    # TO-section: Portuguese gets the positive instruction (use
    # ``eu tinha``, not an apostrophe).
    assert "When translating TO pt-BR" in body
    assert "eu tinha" in body


def test_fr_pt_notes_carry_anti_pattern_examples() -> None:
    """The fr→pt block must show the bug pattern explicitly.

    Second regression report from the same curator: even with the
    abstract "drop the apostrophe" instruction, the model was
    putting orphaned apostrophes in front of words (``por 'ter``,
    ``Eu 'tenho``, ``'ser consolada``). The fix is to teach the
    model with worked examples — the prompt now spells out the
    BAD shape alongside the GOOD shape so the model sees what NOT
    to do, not just what to do. Lock those anchors in so future
    prompt edits don't silently drop them.
    """

    [system, _] = build_translator_messages(
        source_lang="fr",
        target_lang="pt",
        source_text="J'ai une excuse.",
    )
    body = " ".join(system.content.split())
    # Source-side anti-pattern callouts: the curator's exact line
    # shapes from the bug report.
    assert "eu'tinha" in body or "eu 'tinha" in body
    assert "por 'ter dedicado" in body
    assert "'se chamava" in body
    # Target-side: explicit "leading apostrophes are ALWAYS wrong".
    assert "leading apostrophe" in body.lower()
    assert "always wrong" in body.lower()


def test_language_pair_notes_block_omitted_for_unknown_pair() -> None:
    """Unknown source AND unknown target leaves the block empty.

    Most language pairs the curator runs won't have hand-authored
    notes; the prompt should stay compact for that long tail rather
    than mentioning an empty block.
    """

    [system, _] = build_translator_messages(
        source_lang="ja",
        target_lang="ko",
        source_text="hello",
    )
    body = system.content
    # The handoff sentence in rule 2 still mentions
    # ``"Language-pair notes"`` (which is fine — it tells the model
    # to look for the block when present), but the actual rendered
    # block has a parenthesized header (``(ja → ko):``) that must
    # NOT appear when no notes apply.
    assert "Language-pair notes (ja → ko):" not in body
    assert "When translating FROM ja" not in body
    assert "When translating TO ko" not in body


def test_language_pair_notes_block_handles_regional_variants() -> None:
    """``pt-BR`` falls back to the ``pt`` family entry.

    BCP-47 region subtags shouldn't force the curator to author a
    duplicate ``pt-BR`` entry just to get the same Portuguese
    typography reminders. Only deviations from the family default
    need a regional override.
    """

    [system, _] = build_translator_messages(
        source_lang="fr-CA",
        target_lang="pt-BR",
        source_text="Lorsque j'avais six ans.",
    )
    body = " ".join(system.content.split())
    # The FROM line still picks up the family-level French note.
    assert "When translating FROM fr-CA" in body
    assert "j'avais" in body
    # The TO line still picks up the Portuguese note.
    assert "eu tinha" in body


def test_language_pair_notes_block_renders_target_only() -> None:
    """A pair where only the target has notes still gets the block.

    en → pt is a frequent curator pair: English has no apostrophe
    pitfalls worth flagging on the source side, but Portuguese needs
    the positive ``eu tinha`` reminder to keep the model from
    inventing apostrophe contractions on its own.
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt-BR",
        source_text="When I was six.",
    )
    body = " ".join(system.content.split())
    assert "Language-pair notes (en → pt-BR):" in body
    assert "When translating FROM en" not in body
    assert "When translating TO pt-BR" in body
    assert "eu tinha" in body


def test_translator_prompt_warns_against_year_new_entities() -> None:
    """Translator's ``new_entities`` channel must not propose raw years.

    The pipeline-side filter (``_normalize_new_entity`` calls
    :func:`_violates_extractor_caps`) is the load-bearing guard, but
    putting the instruction in the prompt cuts the failure rate at
    the source. Mirror of
    :func:`test_extractor_prompt_warns_against_raw_year_proposals`
    in the extractor suite.
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="The Battle of 1066 changed everything.",
    )
    body = " ".join(system.content.split())
    assert "Do NOT propose raw year references" in body
    assert "1066" in body
    assert "1939-1945" in body
    # The carve-out keeps named eras / phrases legal.
    assert "named" in body


def test_group_translator_prompt_warns_against_year_new_entities() -> None:
    """Same year guard must travel in the grouped prompt."""

    [system, _] = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "The Battle of 1066.")],
    )
    body = " ".join(system.content.split())
    assert "Do NOT propose raw year references" in body
    assert "1066" in body


def test_group_translator_prompt_carries_target_typography_rule() -> None:
    """Same abstract rule + conditional block must travel in grouped prompt.

    Grouped calls handle short, repetitive segments (TOC, lists,
    glossaries) where the model is most tempted to transcribe surface
    punctuation. Keep the rule and conditional block uniform with the
    single-segment template so the contract doesn't fork.
    """

    [system, _] = build_group_translator_messages(
        source_lang="fr",
        target_lang="pt-BR",
        source_items=[(1, "j'avais"), (2, "s'appelait")],
    )
    body = " ".join(system.content.split())
    # Abstract rule.
    assert "target-language conventions" in body
    assert "Language-pair notes" in body
    # Conditional block surfaces fr → pt-BR specifics.
    assert "Language-pair notes (fr → pt-BR):" in body
    assert "j'avais" in body
    assert "eu tinha" in body


def test_glossary_block_segregates_statuses() -> None:
    glossary = [
        GlossaryConstraint(source_term="Élise", target_term="Elisa", status="locked"),
        GlossaryConstraint(
            source_term="London", target_term="Londres", status="confirmed"
        ),
        GlossaryConstraint(
            source_term="Coffer", target_term="Cofre", status="proposed"
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        glossary=glossary,
    )
    locked_idx = system.content.index("locked entries")
    confirmed_idx = system.content.index("confirmed entries")
    proposed_idx = system.content.index("proposed entries")
    assert locked_idx < confirmed_idx < proposed_idx
    assert "Élise → Elisa" in system.content
    assert "London → Londres" in system.content


def test_style_guide_appears_in_system_prompt() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        style_guide="formal register; no contractions",
    )
    assert "formal register" in system.content


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError):
        build_translator_messages(source_lang="en", target_lang="pt", source_text="")


def test_glossary_block_renders_gender_when_present() -> None:
    """Gender flows from the entry through the constraint to the prompt.

    Regression test for the curator-reported article-disagreement
    ("Câmara" rendered as ``o Câmara`` instead of ``a Câmara``). The
    LLM never saw a hint about grammatical gender, so we surface it
    inline next to the target term and a hard rule asks for matching
    articles / agreement.
    """

    glossary = [
        GlossaryConstraint(
            source_term="House",
            target_term="Câmara",
            type="organization",
            status="locked",
            gender="feminine",
            notes="parliament chamber, not a building",
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="The House voted yes.",
        glossary=glossary,
    )
    assert "House → Câmara (gender: feminine)" in system.content
    assert "parliament chamber" in system.content
    # The article-agreement rule travels with the prompt template so
    # the model has explicit instructions for gendered targets.
    assert "MUST agree with that gender" in system.content
    assert "MUST keep the article" in system.content
    # The same-sense rule discourages applying ``House → Câmara`` to a
    # plain "house in the woods".
    assert "same sense as the entry" in system.content


def test_translator_prompt_carries_particle_symmetry_rule() -> None:
    """Hard rule about article/preposition symmetry must travel in the prompt.

    When a glossary entry like ``Europe → Europa`` has no leading
    article, the model is responsible for inflecting "in Europe" to
    "na Europa" and NOT producing "na na Europa". The rule must
    appear verbatim so we can audit prompt drift.
    """

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="In Europe, the Senate voted yes.",
        glossary=[],
    )
    assert "balanced shape" in system.content
    # Anti-doubling clause is the actionable bit.
    assert "na na Europa" in system.content
    assert "the the Senate" in system.content


def test_glossary_block_skips_unspecified_gender() -> None:
    """``unspecified`` is the schema default for "no opinion".

    The block must not add noise for entries that haven't pinned a
    gender (the gendered-target features still need an explicit
    masculine/feminine to fire).
    """

    glossary = [
        GlossaryConstraint(
            source_term="Friend",
            target_term="Amigo",
            gender="unspecified",
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        glossary=glossary,
    )
    # The hard-rules section quotes ``(gender: …)`` as an illustration
    # of the marker shape, so we check only the rendered glossary
    # entry line itself.
    glossary_line = next(
        line for line in system.content.splitlines() if "Friend" in line
    )
    assert "(gender:" not in glossary_line


def test_target_only_block_renders_when_provided() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
            notes="protagonist",
        ),
        TargetOnlyConstraint(
            target_term="Vesemir",
            type="character",
            status="confirmed",
            target_aliases=("velho lobo",),
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" in system.content
    assert "Geralt de Rívia" in system.content
    assert "Vesemir" in system.content
    assert "aliases: velho lobo" in system.content
    assert "MUST translate it using the canonical" in system.content
    locked_idx = system.content.index("locked target forms")
    confirmed_idx = system.content.index("confirmed target forms")
    assert locked_idx < confirmed_idx


def test_target_only_block_omitted_when_empty() -> None:
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
    )
    assert "Canonical target terms used in this work" not in system.content


def test_target_only_block_skips_proposed_only() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Maybe-Term",
            type="term",
            status="proposed",
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="x",
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" not in system.content


def test_group_messages_inject_target_only_block() -> None:
    target_only = [
        TargetOnlyConstraint(
            target_term="Ciri",
            type="character",
            status="locked",
        ),
    ]
    [system, _] = build_group_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_items=[(1, "hello"), (2, "world")],
        target_only_glossary=target_only,
    )
    assert "Canonical target terms used in this work" in system.content
    assert "Ciri" in system.content


def test_translator_prompt_uses_human_readable_language_names() -> None:
    """The opening "Translate from X to Y" line names languages, not just codes.

    Curators reported that ``fr → pt-BR`` in isolation gave the LLM
    less to anchor on than ``French (fr) → Brazilian Portuguese
    (pt-BR)`` — the named form makes the prose register, dialect
    cues, and typography conventions register on the very first
    token. The BCP-47 code stays alongside the name so dialect
    distinctions (``en-GB`` vs ``en-US``) survive.
    """

    [system, _] = build_translator_messages(
        source_lang="fr",
        target_lang="pt-BR",
        source_text="Le petit prince.",
    )
    body = system.content
    assert "from French (fr) to Brazilian Portuguese (pt-BR)" in body


def test_translator_prompt_named_label_handles_regional_variants() -> None:
    """Region-tagged codes get the full regional name when registered."""

    [system, _] = build_translator_messages(
        source_lang="en-GB",
        target_lang="es-MX",
        source_text="hello",
    )
    body = system.content
    assert "from British English (en-GB) to Mexican Spanish (es-MX)" in body


def test_translator_prompt_named_label_falls_back_to_primary_subtag() -> None:
    """``fr-CA`` has no dedicated entry → falls back to the ``fr`` family name."""

    [system, _] = build_translator_messages(
        source_lang="fr-CA",
        target_lang="pt-BR",
        source_text="hello",
    )
    body = system.content
    # fr-CA *is* registered explicitly in the table; check a tag that
    # only matches the primary-subtag fallback path.
    [system2, _] = build_translator_messages(
        source_lang="es-PE",
        target_lang="pt-BR",
        source_text="hello",
    )
    assert "from Canadian French (fr-CA) to Brazilian Portuguese (pt-BR)" in body
    assert "from Spanish (es-PE) to Brazilian Portuguese (pt-BR)" in system2.content


def test_translator_prompt_named_label_preserves_unknown_codes() -> None:
    """A code we don't recognize stays as the bare code (no crash, no fake name)."""

    [system, _] = build_translator_messages(
        source_lang="xx-YY",
        target_lang="zz",
        source_text="hello",
    )
    body = system.content
    assert "from xx-YY to zz" in body


def test_group_translator_prompt_uses_named_languages() -> None:
    """Same name+code rendering must travel in the grouped prompt."""

    [system, _] = build_group_translator_messages(
        source_lang="ja",
        target_lang="en-US",
        source_items=[(1, "konnichiwa")],
    )
    body = system.content
    assert "from Japanese (ja) to American English (en-US)" in body


def test_translator_prompt_context_block_omitted_by_default() -> None:
    """No context arg → no preceding-segments block in the prompt."""

    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="hello",
    )
    body = system.content
    assert "Preceding segments" not in body


def test_translator_prompt_context_block_renders_oldest_first() -> None:
    """Preceding segments render oldest-first with a "do not translate" guard.

    The prompt must be unambiguous: the user message still carries the
    single segment to translate, the context block is reference only.
    The chronological order matches the natural reading order so the
    LLM picks up the recency gradient.
    """

    context = [
        ContextSegment(
            source_text="Bonjour, monsieur.",
            target_text="Olá, senhor.",
            segments_back=2,
        ),
        ContextSegment(
            source_text="Comment allez-vous?",
            target_text="Como vai?",
            segments_back=1,
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="fr",
        target_lang="pt-BR",
        source_text="Très bien, merci.",
        context=context,
    )
    body = system.content
    assert "Preceding segments" in body
    assert "DO NOT translate them" in body
    # Oldest first, newest last.
    older_idx = body.index("Bonjour, monsieur.")
    newer_idx = body.index("Comment allez-vous?")
    assert older_idx < newer_idx
    # Curator-approved targets surface alongside the source so the LLM
    # sees the previously-chosen wording.
    assert "Olá, senhor." in body
    assert "Como vai?" in body


def test_translator_prompt_context_block_marks_untranslated_segments() -> None:
    """A preceding segment without a target prints a clear placeholder.

    A pending preceding segment is still useful as source-side
    context (the model can at least see what came before in the
    original); we just want it labeled rather than silently omitted
    so the model doesn't try to invent a "previous translation".
    """

    context = [
        ContextSegment(
            source_text="Lorem ipsum.",
            target_text=None,
            segments_back=1,
        ),
    ]
    [system, _] = build_translator_messages(
        source_lang="en",
        target_lang="pt",
        source_text="dolor sit amet",
        context=context,
    )
    body = system.content
    assert "Lorem ipsum." in body
    assert "(not yet translated)" in body


def test_parse_well_formed_json() -> None:
    payload = (
        '{"target": "Olá [[T0]]bravo[[/T0]] mundo.", '
        '"used_entries": [], "new_entities": [], "notes": null}'
    )
    trace = parse_translator_response(payload)
    assert trace.target == "Olá [[T0]]bravo[[/T0]] mundo."
    assert trace.used_entries == []
    assert trace.notes is None


def test_parse_recovers_from_prose_wrapping() -> None:
    payload = 'Sure! Here is the JSON:\n{"target": "ok"}\nThanks.'
    trace = parse_translator_response(payload)
    assert trace.target == "ok"


def test_parse_used_entries_and_new_entities() -> None:
    payload = (
        '{"target": "Elisa caminhou.", '
        '"used_entries": ["Élise"], '
        '"new_entities": [{"type": "place", "source": "Vale Verde"}], '
        '"notes": "first appearance"}'
    )
    trace = parse_translator_response(payload)
    assert trace.used_entries == ["Élise"]
    assert trace.new_entities == [{"type": "place", "source": "Vale Verde"}]
    assert trace.notes == "first appearance"


def test_parse_rejects_garbage() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("this is not even close to JSON")


def test_parse_rejects_empty() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("")


def test_parse_rejects_missing_target() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"used_entries": []}')


def test_parse_rejects_non_string_target() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": 123}')


def test_parse_rejects_non_object_root() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response("[1, 2, 3]")


def test_parse_rejects_invalid_used_entries_type() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": "x", "used_entries": "nope"}')


def test_parse_rejects_invalid_new_entity_shape() -> None:
    with pytest.raises(LLMResponseError):
        parse_translator_response('{"target": "x", "new_entities": ["not-a-dict"]}')
