"""Pydantic shape tests for glossary models (PRD §4.3 / M3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from epublate.glossary.models import (
    GlossaryAlias,
    GlossaryEntry,
    GlossaryEntryWithAliases,
)


def _entry(**overrides: object) -> GlossaryEntry:
    base: dict[str, object] = {
        "id": "e1",
        "project_id": "p1",
        "type": "character",
        "source_term": "Élise",
        "target_term": "Elisa",
        "status": "confirmed",
    }
    base.update(overrides)
    return GlossaryEntry(**base)  # type: ignore[arg-type]


def test_entry_defaults_and_required_fields() -> None:
    entry = _entry()
    assert entry.gender is None
    assert entry.created_at == 0
    assert entry.updated_at == 0


def test_entry_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GlossaryEntry(
            id="x",
            project_id="p",
            source_term="a",
            target_term="b",
            unexpected="boom",  # type: ignore[arg-type]
        )


def test_entry_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        _entry(status="bogus")


def test_alias_must_pick_known_side() -> None:
    GlossaryAlias(id="a1", entry_id="e1", side="source", text="Lise")
    with pytest.raises(ValidationError):
        GlossaryAlias(id="a1", entry_id="e1", side="weird", text="Lise")  # type: ignore[arg-type]


def test_with_aliases_dedups_canonical_term() -> None:
    composite = GlossaryEntryWithAliases(
        entry=_entry(),
        source_aliases=["Élise", "Lise", "Lise"],
        target_aliases=["Elisa", "Eli"],
    )
    # ``all_source_terms`` puts the canonical first and dedups aliases.
    assert composite.all_source_terms() == ["Élise", "Lise"]
    assert composite.all_target_terms()[0] == "Elisa"
    assert "Eli" in composite.all_target_terms()


def test_status_property_proxies_entry() -> None:
    composite = GlossaryEntryWithAliases(entry=_entry(status="locked"))
    assert composite.status == "locked"
    assert composite.id == composite.entry.id
    assert composite.source_term == composite.entry.source_term
