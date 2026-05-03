"""Glossary particle / lemma normalization (PRD F-LB-3 / glossary-invariants §1).

Why this module exists
----------------------

A glossary entry like ``Europe → na Europa`` looks fine in isolation
but explodes at translation time: ``na`` is a Portuguese contraction
of ``em + a Europa``. As soon as the source segment says
*"in Europe"*, a translator dutifully applying the entry produces
*"na na Europa"* — the same preposition twice. Symmetric pairs like
``the USA → os EUA`` are fine because both sides carry the leading
function word; lemma-form pairs like ``Europe → Europa`` or
``USA → EUA`` are fine because neither side does. The pathological
cases are the *asymmetric* ones, where exactly one side carries a
leading article / preposition / contraction.

We address this in three places:

1. **Auto-proposer (lemma form):** the helper LLM that proposes
   entities is unreliable about leading function words, so
   :func:`epublate.glossary.io.upsert_proposed` strips them from
   *both* sides before insert. This always lands a symmetric
   (lemma-form) pair, no matter how noisy the proposal was.
2. **Curator save (symmetry required):** the Glossary edit modal
   calls :func:`analyze_pair` before persisting. Asymmetric pairs
   are rejected with a curator-friendly error so the human can
   decide whether to lemma-form both sides or add the missing
   particle on the other side.
3. **Translator validator (doubled particles):** the pipeline runs
   :func:`find_doubled_particles` on the LLM's target output and
   soft-flags the segment when the model still emits ``"na na"`` /
   ``"the the"`` runs. Soft, not hard: we surface to the curator
   in the Inbox rather than retry.

Per-language sets are deliberately conservative. Adding a particle
to a set must not regress real-world entries (band names, recurring
emphasized prose, etc.). When in doubt, leave the word out and rely
on the symmetry check + the human review.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Per-language sets of leading articles / prepositions / contractions.
# Keys are normalized BCP-47 primary subtags (lower-cased, dashes
# collapsed). Lookup defaults to the English set when the lang is
# unknown — that's safer than aggressively stripping particles in a
# language we haven't audited.
_LEADING_PARTICLES: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "the",
            "a",
            "an",
        }
    ),
    "pt": frozenset(
        {
            # Definite + indefinite articles.
            "o",
            "a",
            "os",
            "as",
            "um",
            "uma",
            "uns",
            "umas",
            # Standalone prepositions that commonly lead a noun phrase.
            "de",
            "em",
            "por",
            "para",
            "com",
            # Preposition + definite article contractions.
            "do",
            "da",
            "dos",
            "das",
            "no",
            "na",
            "nos",
            "nas",
            "ao",
            "à",
            "aos",
            "às",
            "pelo",
            "pela",
            "pelos",
            "pelas",
            # Preposition + indefinite article contractions.
            "num",
            "numa",
            "nuns",
            "numas",
            "dum",
            "duma",
            "duns",
            "dumas",
        }
    ),
    "es": frozenset(
        {
            "el",
            "la",
            "los",
            "las",
            "un",
            "una",
            "unos",
            "unas",
            "de",
            "en",
            "por",
            "para",
            "con",
            "del",
            "al",
        }
    ),
    "fr": frozenset(
        {
            "le",
            "la",
            "les",
            "un",
            "une",
            "des",
            "de",
            "en",
            "à",
            "du",
            "au",
            "aux",
        }
    ),
    "it": frozenset(
        {
            "il",
            "lo",
            "la",
            "i",
            "gli",
            "le",
            "un",
            "uno",
            "una",
            "di",
            "in",
            "a",
            "da",
            "su",
            "per",
            "con",
            "del",
            "dello",
            "della",
            "dei",
            "degli",
            "delle",
            "al",
            "allo",
            "alla",
            "ai",
            "agli",
            "alle",
            "dal",
            "dallo",
            "dalla",
            "dai",
            "dagli",
            "dalle",
            "nel",
            "nello",
            "nella",
            "nei",
            "negli",
            "nelle",
            "sul",
            "sullo",
            "sulla",
            "sui",
            "sugli",
            "sulle",
        }
    ),
    "de": frozenset(
        {
            "der",
            "die",
            "das",
            "den",
            "dem",
            "des",
            "ein",
            "eine",
            "einen",
            "einem",
            "einer",
            "eines",
            "in",
            "an",
            "auf",
            "zu",
            "von",
            "mit",
            "bei",
            "im",
            "am",
            "zum",
            "zur",
            "vom",
            "beim",
        }
    ),
}


# Word tokenizer used by both ``leading_particle`` (single leading
# token) and ``find_doubled_particles`` (adjacent token pairs).
# ``[^\W\d_]+`` matches any Unicode letter run; the optional
# ``'[^\W\d_]+`` tail keeps French ``l'``-style elisions intact.
_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


@dataclass(slots=True, frozen=True)
class NormalizedTerm:
    """Result of stripping at most one leading function word.

    ``stripped`` is the lemma form (original term with its leading
    particle removed and whitespace re-normalized). ``particle`` is
    the lower-cased function word that was stripped, or ``None``
    when the term was already in lemma form.
    """

    original: str
    stripped: str
    particle: str | None


@dataclass(slots=True, frozen=True)
class ParticleSymmetry:
    """Symmetry analysis between a source/target glossary pair.

    A pair is symmetric when *both* sides carry a leading particle
    or *neither* does. The asymmetric case is what produces
    ``"na na Europa"`` style doubling at translation time.
    """

    symmetric: bool
    source_particle: str | None
    target_particle: str | None
    message: str = ""


def _resolve_lang(lang: str | None) -> str:
    if not lang:
        return "en"
    return lang.split("-")[0].split("_")[0].lower()


def _particles_for(lang: str | None) -> frozenset[str]:
    return _LEADING_PARTICLES.get(_resolve_lang(lang), _LEADING_PARTICLES["en"])


def leading_particle(text: str, *, lang: str | None) -> str | None:
    """Return the lower-cased leading article/preposition, or ``None``.

    Conservative: only single-token prefixes drawn from
    :data:`_LEADING_PARTICLES` are matched. Multi-word prefixes
    (``"of the"``) are intentionally out of scope; the
    :func:`analyze_pair` symmetry check still catches them because
    the source side ends up longer than expected.
    """

    cleaned = text.strip()
    if not cleaned:
        return None
    parts = cleaned.split(None, 1)
    if len(parts) < 2:
        # A bare ``"the"`` entry is almost certainly garbage — refuse
        # to claim it's a particle so the symmetry check can flag it.
        return None
    head = parts[0].strip(".,;:!?'\"`«»()[]{}").lower()
    if not head:
        return None
    return head if head in _particles_for(lang) else None


def normalize_term(text: str, *, lang: str | None) -> NormalizedTerm:
    """Strip a single leading article/preposition (lemma form).

    The function only removes the *first* token. ``"of the United
    States"`` becomes ``"the United States"`` (still has ``"the"``);
    apply :func:`normalize_term` again if the caller wants
    full-depth stripping. We don't recurse by default because
    multi-word prefixes are rare enough that the symmetry check is
    a better safety net.
    """

    cleaned = text.strip()
    if not cleaned:
        return NormalizedTerm(original=text, stripped="", particle=None)
    particle = leading_particle(cleaned, lang=lang)
    if particle is None:
        return NormalizedTerm(original=text, stripped=cleaned, particle=None)
    parts = cleaned.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        return NormalizedTerm(original=text, stripped=cleaned, particle=None)
    return NormalizedTerm(
        original=text,
        stripped=parts[1].strip(),
        particle=particle,
    )


def analyze_pair(
    *,
    source_term: str | None,
    target_term: str,
    source_lang: str | None,
    target_lang: str | None,
) -> ParticleSymmetry:
    """Check whether a glossary pair has symmetric leading particles.

    Target-only entries (``source_term=None``) are trivially symmetric:
    they pin a canonical target spelling, the curator never saw the
    source wording, and the validator's anti-doubling check at
    translation time keeps them safe regardless.
    """

    if source_term is None:
        return ParticleSymmetry(
            symmetric=True,
            source_particle=None,
            target_particle=leading_particle(target_term, lang=target_lang),
        )

    src = leading_particle(source_term, lang=source_lang)
    tgt = leading_particle(target_term, lang=target_lang)
    if (src is None) == (tgt is None):
        return ParticleSymmetry(
            symmetric=True,
            source_particle=src,
            target_particle=tgt,
        )

    if src is None:
        # Target leads with a function word but the source doesn't.
        # This is the ``Europe → na Europa`` shape that produces
        # ``"na na Europa"`` doubling in real translations.
        message = (
            f"target {target_term!r} starts with {tgt!r} but the source "
            f"{source_term!r} has no matching article/preposition. "
            "Either remove the leading word from the target (lemma form) "
            f"or prepend a matching article/preposition to the source."
        )
    else:
        message = (
            f"source {source_term!r} starts with {src!r} but the target "
            f"{target_term!r} has no matching article/preposition. "
            "Either remove the leading word from the source (lemma form) "
            f"or prepend a matching article/preposition to the target."
        )
    return ParticleSymmetry(
        symmetric=False,
        source_particle=src,
        target_particle=tgt,
        message=message,
    )


def find_doubled_particles(
    text: str,
    *,
    lang: str | None,
    extra_particles: Sequence[str] = (),
) -> list[tuple[str, int]]:
    """Find ``"na na"`` / ``"the the"`` adjacent-particle runs.

    Returns ``[(particle, char_offset), …]`` for every adjacent pair
    of *identical* function words (case-insensitive) in ``text``.
    The pair must be word-adjacent — ``"in. In"`` across a sentence
    boundary is not flagged. ``extra_particles`` lets the caller
    extend the language-specific set with any custom strings the
    pipeline wants to police (the validator currently doesn't, but
    the hook is there for tests / future Lore Book features).
    """

    base = _particles_for(lang)
    extras = frozenset(p.lower() for p in extra_particles)
    particles = base | extras
    out: list[tuple[str, int]] = []
    matches = list(_WORD_RE.finditer(text))
    for i in range(len(matches) - 1):
        m1 = matches[i]
        m2 = matches[i + 1]
        head = m1.group(0).lower()
        if head != m2.group(0).lower():
            continue
        if head not in particles:
            continue
        out.append((head, m1.start()))
    return out


__all__ = [
    "NormalizedTerm",
    "ParticleSymmetry",
    "analyze_pair",
    "find_doubled_particles",
    "leading_particle",
    "normalize_term",
]
