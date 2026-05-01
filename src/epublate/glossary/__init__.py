"""Glossary (lore bible) — PRD §4.3 / M3.

The lore bible is the heart of consistency: it tells the LLM how to
translate proper nouns and recurring phrases the same way across an
entire book. Every glossary write goes through :mod:`epublate.db.repo`
helpers; the modules here are pure value objects, matchers, validators,
and the cascade flow.

See ``.cursor/rules/glossary-invariants.mdc`` for the hard rules.
"""

from epublate.glossary.cascade import (
    CascadeCandidate,
    cascade_retranslate,
    compute_affected,
)
from epublate.glossary.enforcer import (
    Violation,
    ViolationSeverity,
    build_constraints,
    find_mentions,
    glossary_hash,
    has_locked_violation,
    validate_target,
)
from epublate.glossary.matcher import Match, make_pattern, match_source, target_uses
from epublate.glossary.models import (
    AliasSide,
    EntityMention,
    EntityType,
    GenderTag,
    GlossaryAlias,
    GlossaryEntry,
    GlossaryEntryWithAliases,
    GlossaryRevision,
    GlossaryStatus,
    GlossaryStatusLiteral,
)

__all__ = [
    "AliasSide",
    "CascadeCandidate",
    "EntityMention",
    "EntityType",
    "GenderTag",
    "GlossaryAlias",
    "GlossaryEntry",
    "GlossaryEntryWithAliases",
    "GlossaryRevision",
    "GlossaryStatus",
    "GlossaryStatusLiteral",
    "Match",
    "Violation",
    "ViolationSeverity",
    "build_constraints",
    "cascade_retranslate",
    "compute_affected",
    "find_mentions",
    "glossary_hash",
    "has_locked_violation",
    "make_pattern",
    "match_source",
    "target_uses",
    "validate_target",
]
