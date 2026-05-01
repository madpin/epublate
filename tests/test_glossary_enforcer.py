"""Tests for the glossary enforcer (PRD §4.2 phase 5 / M3)."""

from __future__ import annotations

from epublate.glossary.enforcer import (
    build_constraints,
    glossary_hash,
    has_locked_violation,
    validate_target,
)
from epublate.glossary.models import GlossaryEntry, GlossaryEntryWithAliases


def _entry(
    source: str,
    target: str,
    *,
    status: str = "confirmed",
    src_aliases: list[str] | None = None,
    tgt_aliases: list[str] | None = None,
    eid: str = "e1",
    notes: str | None = None,
) -> GlossaryEntryWithAliases:
    return GlossaryEntryWithAliases(
        entry=GlossaryEntry(
            id=eid,
            project_id="p",
            type="character",
            source_term=source,
            target_term=target,
            status=status,  # type: ignore[arg-type]
            notes=notes,
        ),
        source_aliases=src_aliases or [],
        target_aliases=tgt_aliases or [],
    )


def test_build_constraints_skips_proposed() -> None:
    entries = [
        _entry("Élise", "Elisa", status="proposed", eid="a"),
        _entry("Hugo", "Hugo", status="confirmed", eid="b"),
        _entry("Léon", "Leon", status="locked", eid="c"),
    ]
    out = build_constraints(entries)
    sources = [c.source_term for c in out]
    statuses = [c.status for c in out]
    assert sources == ["Léon", "Hugo"]  # locked first, then confirmed
    assert statuses == ["locked", "confirmed"]


def test_validate_target_locked_violation_is_error() -> None:
    entries = [_entry("Élise", "Elisa", status="locked")]
    violations = validate_target(
        source_text="Élise smiled.",
        target_text="Elise sorriu.",  # missing canonical "Elisa"
        entries=entries,
    )
    assert len(violations) == 1
    assert violations[0].severity == "error"
    assert has_locked_violation(violations)


def test_validate_target_confirmed_violation_is_warning() -> None:
    entries = [_entry("Élise", "Elisa", status="confirmed")]
    violations = validate_target(
        source_text="Élise smiled.",
        target_text="Liz sorriu.",
        entries=entries,
    )
    assert len(violations) == 1
    assert violations[0].severity == "warning"
    assert not has_locked_violation(violations)


def test_validate_target_no_violation_when_target_uses_canonical() -> None:
    entries = [_entry("Élise", "Elisa", status="locked")]
    violations = validate_target(
        source_text="Élise smiled.",
        target_text="Elisa sorriu.",
        entries=entries,
    )
    assert violations == []


def test_validate_target_target_alias_satisfies_locked() -> None:
    entries = [
        _entry("Élise", "Elisa", status="locked", tgt_aliases=["Eli"]),
    ]
    violations = validate_target(
        source_text="Élise nodded.",
        target_text="Eli nodded.",
        entries=entries,
    )
    assert violations == []


def test_validate_target_skips_when_source_term_absent() -> None:
    entries = [_entry("Élise", "Elisa", status="locked")]
    violations = validate_target(
        source_text="Mary nodded.",
        target_text="Mary nodded.",
        entries=entries,
    )
    assert violations == []


def test_validate_target_proposed_never_violates() -> None:
    entries = [_entry("Élise", "Elisa", status="proposed")]
    violations = validate_target(
        source_text="Élise nodded.",
        target_text="Anything goes.",
        entries=entries,
    )
    assert violations == []


def test_glossary_hash_is_deterministic() -> None:
    a = [_entry("Élise", "Elisa", eid="a"), _entry("Hugo", "Hugo", eid="b")]
    b = [_entry("Hugo", "Hugo", eid="b"), _entry("Élise", "Elisa", eid="a")]
    assert glossary_hash(a) == glossary_hash(b)


def test_glossary_hash_changes_with_target_term() -> None:
    a = [_entry("Élise", "Elisa", eid="a")]
    b = [_entry("Élise", "Elise", eid="a")]
    assert glossary_hash(a) != glossary_hash(b)


def test_glossary_hash_changes_with_status() -> None:
    a = [_entry("Élise", "Elisa", eid="a", status="confirmed")]
    b = [_entry("Élise", "Elisa", eid="a", status="locked")]
    assert glossary_hash(a) != glossary_hash(b)


def test_glossary_hash_changes_with_aliases() -> None:
    a = [_entry("Élise", "Elisa", eid="a")]
    b = [_entry("Élise", "Elisa", eid="a", src_aliases=["Lise"])]
    assert glossary_hash(a) != glossary_hash(b)


def test_glossary_hash_stable_across_id_change() -> None:
    a = [_entry("Élise", "Elisa", eid="a")]
    b = [_entry("Élise", "Elisa", eid="other-id")]
    assert glossary_hash(a) == glossary_hash(b)
