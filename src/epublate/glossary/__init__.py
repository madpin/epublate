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
from epublate.glossary.dedup import canonical_form, find_near_duplicates
from epublate.glossary.enforcer import (
    Violation,
    ViolationKind,
    ViolationSeverity,
    build_constraints,
    find_mentions,
    find_target_doubled_particles,
    glossary_hash,
    has_flagging_violation,
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
from epublate.glossary.normalize import (
    NormalizedTerm,
    ParticleSymmetry,
    analyze_pair,
    find_doubled_particles,
    leading_particle,
    normalize_term,
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
    "NormalizedTerm",
    "ParticleSymmetry",
    "Violation",
    "ViolationKind",
    "ViolationSeverity",
    "analyze_pair",
    "build_constraints",
    "canonical_form",
    "cascade_retranslate",
    "compute_affected",
    "find_doubled_particles",
    "find_mentions",
    "find_near_duplicates",
    "find_target_doubled_particles",
    "glossary_hash",
    "has_flagging_violation",
    "has_locked_violation",
    "leading_particle",
    "make_pattern",
    "match_source",
    "normalize_term",
    "target_uses",
    "validate_target",
]
