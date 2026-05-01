"""Tone / style profile registry (PRD F-STYLE-1).

The translator's system prompt has always carried a free-form ``style_guide``
string (PRD §8.1, ``epublate.llm.prompts.translator``). Authoring that
paragraph from scratch every time is a bad UX, so this module ships a small
catalog of named **style profiles** the curator picks at project-creation
time. Each profile expands to a pre-written paragraph the LLM understands
("middle-grade adventure, sentence rhythm tuned for 9-12 year olds...").

Two ways the curator's choice lands in the project:

* :func:`resolve_style_guide` turns a ``(profile_id, custom_text)`` pair
  into the actual prompt block we persist on ``project.style_guide``.
  Custom text wins over the preset — the New Project modal pre-fills the
  TextArea with the preset's text but lets the curator edit it.
* :data:`DEFAULT_STYLE_PROFILE` is the safe fallback we plant on freshly
  created projects so translations don't drift into a sterile, register-
  neutral default.

The helper LLM extractor (M5) returns a best-effort ``(register, audience)``
observation about the book; :func:`suggest_style_profile` maps that pair
to a preset id so the dashboard can surface "Helper suggests: YA fantasy"
right after intake.

Profiles intentionally avoid mechanical rules ("no contractions" / "use
formal address" / etc.) because:

* The PRD's S3 framing of a style-guide DSL is post-v1 territory; for
  v1 we want goal-oriented presets a curator can pick in two seconds.
* The LLM responds better to short, vivid descriptions of audience and
  tone than to a checklist of micro-rules. Curators who need fine
  control can edit the prose directly via the New Project modal or
  the Settings → Style guide screen.

Cache discipline: the profile's ``prompt_block`` is what lives in
``project.style_guide``, which is part of the translator's system prompt
hash — so changing a profile correctly invalidates cached translations
(PRD F-LLM-6). Callers don't need to do anything special.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_STYLE_PROFILE = "literary_fiction"
"""Safe fallback profile applied when no style is explicitly set.

A literary register reads naturally for adult novels and is the least
likely to feel jarring against an unknown book; the alternative
(``None`` / no style guide) tends to produce a sterile, dictionary-
voiced translation that flattens the source's tone."""


@dataclass(slots=True, frozen=True)
class StyleProfile:
    """One named tone preset shown to the curator.

    The four attributes serve different audiences: ``id`` is the
    machine-readable slug we persist; ``name`` is the short label the
    Select widget shows; ``description`` is the one-liner that lands on
    the dashboard / settings panel; ``prompt_block`` is the actual
    paragraph the translator's system prompt embeds.
    """

    id: str
    name: str
    description: str
    prompt_block: str


def _normalize(text: str) -> str:
    """Collapse paragraph indentation so the prompt block reads cleanly.

    The string literals below are written with leading spaces for source
    readability; the model sees them prefixed by "Style guide:" already,
    so trimming each line keeps the prompt tight without losing the
    paragraph structure.
    """

    lines = [line.strip() for line in text.strip().splitlines()]
    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


_PROFILES: tuple[StyleProfile, ...] = (
    StyleProfile(
        id="literary_fiction",
        name="Literary fiction",
        description=(
            "Adult literary register. Preserves narrative voice and "
            "subtext. Safe default for unknown books."
        ),
        prompt_block=_normalize(
            """
            Translate as adult literary fiction. Preserve the narrator's
            voice, sentence rhythm, and subtext; do not flatten metaphors
            into literal meaning. Mirror the source's register from
            paragraph to paragraph rather than imposing a uniform formal
            tone — when the source shifts (dialogue vs. exposition,
            character thought vs. action), let the translation shift with
            it. Keep contractions and colloquialisms when the source uses
            them; keep formal or archaic phrasing when it doesn't.
            """
        ),
    ),
    StyleProfile(
        id="classic_literature",
        name="Classic literature (19th - early 20th c.)",
        description=(
            "Nineteenth to early twentieth century register. Long "
            "articulated sentences, formal diction, omniscient voice."
        ),
        prompt_block=_normalize(
            """
            Translate as classic nineteenth- to early-twentieth-century
            literature. Sentences are long and articulated, with
            parenthetical asides and a confident omniscient narrator.
            Diction is formal and precise; elevated vocabulary a
            21st-century reader finds old-fashioned (earnestly,
            whereupon, countenance, acquainted) is appropriate and
            should be preserved rather than modernized. Dialogue uses
            the period's politeness formulas — honorifics, titles, set
            forms of address — unless the source deliberately subverts
            them. Do not contract or compress for pace; the ornate
            rhythm is part of the voice and the period marker.
            """
        ),
    ),
    StyleProfile(
        id="historical_fiction",
        name="Historical fiction (period-set)",
        description=(
            "Period-set storytelling. Era-appropriate voice and "
            "vocabulary with no modern anachronism."
        ),
        prompt_block=_normalize(
            """
            Translate as historical fiction set in a specific era.
            Honor period-appropriate vocabulary, idiom, and rhetorical
            cadence without slipping into modern colloquialism or
            anachronism. Place names, institutions, titles of office,
            weights and measures, currencies, and technology must
            reflect the source era; do not silently modernize them.
            When the source uses period dialect — peasant speech,
            courtly speech, soldier's argot — approximate it in the
            target language with the equivalent register rather than
            flattening to contemporary neutral prose. Keep the
            narrator's distance: a 19th-century omniscient narrator
            stays 19th-century, a close-third contemporary narrator
            looking back stays contemporary.
            """
        ),
    ),
    StyleProfile(
        id="children_picture",
        name="Children - picture book / early readers",
        description=("Short, concrete sentences for ages ~3-7. Read-aloud cadence."),
        prompt_block=_normalize(
            """
            Translate for very young readers (roughly ages 3-7) and for an
            adult reading aloud. Prefer short, concrete sentences with a
            clear musical rhythm. Use vocabulary a small child already
            knows; gloss any unavoidable rare word in-line. Keep
            onomatopoeia, repetition, and rhyme when the source uses them
            — re-rhyme in the target language even if it requires a small
            departure from the literal source phrasing. Avoid irony,
            sarcasm, or jargon.
            """
        ),
    ),
    StyleProfile(
        id="middle_grade",
        name="Middle grade (ages 8-12)",
        description=("Clear, brisk prose for 8-12 year olds. Light humor allowed."),
        prompt_block=_normalize(
            """
            Translate for middle-grade readers (roughly ages 8-12).
            Sentences are clear and brisk, with concrete imagery and a
            warm narrator voice. Light humor is welcome; cynicism and
            graphic content are not. Vocabulary should be slightly above
            a child's everyday speech but never gatekeeping — when the
            source uses a difficult word with intent, keep it; when it
            uses one without intent, prefer the simpler equivalent.
            Honorifics, slang, and pop-culture references should match
            the target culture's middle-grade norms.
            """
        ),
    ),
    StyleProfile(
        id="young_adult",
        name="Young adult (teen / YA)",
        description=(
            "Contemporary teen voice. Punchy dialogue, real emotional stakes."
        ),
        prompt_block=_normalize(
            """
            Translate as young-adult fiction for a teen audience. The
            narrator's voice and any first-person POV should feel
            current and emotionally honest — neither overly sanitized
            nor performatively edgy. Dialogue is punchy and contracted;
            interiority is allowed to be raw. Mild profanity, romantic
            tension, and difficult emotional content should land with
            the same intensity they have in the source — do not soften
            them, but do not amplify them either. Slang should match
            the target language's contemporary teen register.
            """
        ),
    ),
    StyleProfile(
        id="fairytale_folklore",
        name="Fairytale / folklore / mythology",
        description=(
            "Oral-tradition cadence. Formulaic openings, archaic "
            "phrasing, moral weight carried by rhythm."
        ),
        prompt_block=_normalize(
            """
            Translate as fairytale, folklore, fable, or mythology.
            The register is oral and archaic: formulaic openings
            ("Once upon a time", "In the days when beasts could
            speak"), stock epithets, and refrain-like repetition are
            structural features, not mannerisms — preserve them with
            the target language's equivalent traditional formulas.
            Vocabulary leans toward plain nouns and strong verbs;
            moral weight is carried by rhythm and repetition rather
            than by explicit narration. Character archetypes (the
            youngest son, the old woman at the crossroads, the
            ungrateful king) retain their flat, emblematic quality.
            Magical or sacred thresholds — three trials, seven years,
            a river the hero must not cross — are preserved verbatim;
            do not update the numbers or symbols.
            """
        ),
    ),
    StyleProfile(
        id="genre_fiction",
        name="Adult genre fiction (thriller / fantasy / SF)",
        description=("Pacey adult genre prose. Vivid action, clean dialogue beats."),
        prompt_block=_normalize(
            """
            Translate as adult genre fiction (thriller, fantasy, science
            fiction, mystery, etc.). Keep the prose pacey: short
            sentences in action, longer sentences in interiority and
            world-building. Preserve invented terminology exactly as
            given by the glossary; do not 'normalize' magical, technical,
            or sci-fi vocabulary. Dialogue should feel natural and
            character-specific. Violence and tension land with the
            source's intensity; do not soften.
            """
        ),
    ),
    StyleProfile(
        id="noir_crime",
        name="Noir / hard-boiled crime",
        description=(
            "Terse, cynical register. Clipped dialogue, urban grit, stoic interiority."
        ),
        prompt_block=_normalize(
            """
            Translate as noir or hard-boiled crime fiction. The prose
            is terse, stylized, and worldly-cynical. Sentences run
            short; dialogue is clipped, frequently interrupted, and
            attributed with simple "said" verbs rather than ornamental
            speech tags. Urban geography is specific and often grim;
            keep street names, districts, and period brands verbatim
            unless the glossary localizes them. Period slang ("dame",
            "mug", "rap sheet") lands with an era-equivalent idiom in
            the target language rather than a gloss. Metaphors are
            concrete and bruising; interiority is stoic and
            understated — first-person narrators confess by
            implication, not by monologue. Violence lands with the
            source's matter-of-fact intensity; do not soften.
            """
        ),
    ),
    StyleProfile(
        id="horror_gothic",
        name="Horror / gothic",
        description=(
            "Atmospheric dread. Controlled pacing, sensory detail, "
            "deliberate ambiguity."
        ),
        prompt_block=_normalize(
            """
            Translate as horror or gothic fiction. The register is
            atmospheric and patient: dread is built from sensory
            detail, controlled pacing, and silences rather than
            explicit shock. Let long, lulling sentences in descriptive
            passages do their hypnotic work; let short, stark
            sentences at the turn land with impact. Preserve
            body-horror and uncanny specifics verbatim — do not soften
            visceral detail — and preserve the deliberate ambiguity
            of unreliable narrators (what was, and what might have
            been). Archaic or regional vocabulary that heightens the
            uncanny (gothic-era diction, folk-horror dialect) should
            land with a period-appropriate equivalent in the target
            language rather than being modernized away.
            """
        ),
    ),
    StyleProfile(
        id="cozy_romance",
        name="Romance — cozy / closed-door",
        description=(
            "Warm romantic register. Emotional intimacy without explicit content."
        ),
        prompt_block=_normalize(
            """
            Translate as a cozy / closed-door romance. The register is
            warm, intimate, and emotionally generous; the focus is on
            longing, banter, and emotional vulnerability rather than
            physical detail. When the source describes physical intimacy
            it does so by implication — keep that implication intact;
            do not add explicit detail and do not blur over what the
            source explicitly shows. Inner monologue is welcome.
            Endearments and pet names should land naturally in the
            target language's romantic vocabulary.
            """
        ),
    ),
    StyleProfile(
        id="explicit_adult",
        name="Adult — explicit / erotic",
        description=(
            "Adult-only. Explicit physical and sexual content is preserved verbatim."
        ),
        prompt_block=_normalize(
            """
            Translate as adult fiction for an adult audience. Explicit
            sexual, sensual, or graphic content must be preserved in
            full — do not soften, paraphrase, or summarize physical
            description, anatomical vocabulary, or sexual acts.
            Consensual, non-consensual, and morally complex situations
            in the source must land with the same explicitness in the
            target. Match the source's register: when it is filthy or
            casual, the translation is filthy or casual; when it is
            literary or tender, the translation is literary or tender.
            Do not add disclaimers, warnings, or content notices that
            are not in the source.
            """
        ),
    ),
    StyleProfile(
        id="humor_comedy",
        name="Humor / comedy",
        description=(
            "Comic timing is the product. Wordplay, understatement, "
            "and rhythm over literal accuracy."
        ),
        prompt_block=_normalize(
            """
            Translate as humor or comedy. The product is the laugh;
            when a literal translation kills the joke, rewrite the
            joke so it lands in the target language. Puns, wordplay,
            running gags, and comic misnaming are structural — find
            target-language equivalents rather than preserving the
            source words verbatim. Understatement, deadpan delivery,
            and comic timing (the set-up / pause / punch-line rhythm)
            must survive even when every individual word changes.
            Cultural references the target reader wouldn't recognize
            get naturalized to an equivalent the reader will
            recognize, unless the joke is specifically about the
            foreignness itself. Keep the narrator's attitude —
            bemused, caustic, affectionate, absurdist — consistent
            from scene to scene.
            """
        ),
    ),
    StyleProfile(
        id="memoir_biography",
        name="Memoir / biography",
        description=(
            "Reflective nonfiction. First-person voice, scene-and-"
            "summary rhythm, controlled emotion."
        ),
        prompt_block=_normalize(
            """
            Translate as memoir or biography. The register is
            reflective and voice-forward: a first-person (or close
            third, for biography) narrator looks back on lived events
            with emotional distance and occasional candor. Keep the
            signature tics of the original voice — sentence length,
            digressions, self-deprecating asides — intact even when
            they would be trimmed in fiction. Private names,
            nicknames, and family idiolect stay verbatim unless the
            glossary specifies otherwise. Quoted remembered speech
            lands in the target language's colloquial register
            without losing the specificity of the remembered voice.
            Factual specifics — dates, places, titles, institutions
            — are precise and not stylized away.
            """
        ),
    ),
    StyleProfile(
        id="poetry_verse",
        name="Poetry / verse",
        description=(
            "Verse form is the text. Meter, line breaks, and sound "
            "patterns take priority over literalism."
        ),
        prompt_block=_normalize(
            """
            Translate as poetry or verse. The formal features —
            meter, line breaks, stanza shape, rhyme scheme, internal
            sound patterns — are load-bearing, not decorative.
            Preserve line and stanza boundaries exactly; preserve the
            rhyme scheme (A/B/A/B, couplets, etc.) in the target
            language even at the cost of literal word-for-word
            fidelity. Meter should match the source's pulse (iambic,
            syllabic, free) as naturally as the target language
            allows. Figurative language — metaphor, metonymy,
            ambiguity — survives as ambiguity; do not resolve double
            meanings into a single reading. Titles, epigraphs, and
            dedications carry the same weight as body text.
            """
        ),
    ),
    StyleProfile(
        id="religious_spiritual",
        name="Religious / spiritual",
        description=(
            "Reverent register. Liturgical vocabulary, canonical "
            "phrasing, quoted scripture verbatim."
        ),
        prompt_block=_normalize(
            """
            Translate as religious or spiritual prose. The register is
            reverent and carefully weighted; the target language's
            established liturgical vocabulary (for prayer, scripture,
            ritual, clerical titles) is preferred over neutral
            everyday synonyms. Quoted scripture or recognized sacred
            passages should match the canonical translation used in
            the target tradition when one exists; do not paraphrase
            them. Honorifics, names of the divine, and titles of
            clergy follow the target tradition's capitalization and
            form. Theological terminology is precise and not to be
            loosened (incarnation ≠ embodiment; grace ≠ kindness).
            Preserve the source's tone — contemplative, devotional,
            admonitory, celebratory — paragraph by paragraph.
            """
        ),
    ),
    StyleProfile(
        id="technical_manual",
        name="Technical manual / how-to",
        description=("Precise, instructional. Terminology is non-negotiable."),
        prompt_block=_normalize(
            """
            Translate as a technical manual or how-to. Precision over
            elegance: prefer the unambiguous wording even when it is
            less literary. Domain terminology, command names, function
            signatures, units, and product names must land verbatim
            unless the glossary specifies a localized term. Use the
            target language's standard imperative form for instructions.
            Numbered steps, code blocks, and notes/warnings retain their
            structure exactly. Do not introduce stylistic flourishes the
            source does not have.
            """
        ),
    ),
    StyleProfile(
        id="academic",
        name="Academic / scholarly",
        description=(
            "Formal scholarly register. Citations and hedged claims preserved."
        ),
        prompt_block=_normalize(
            """
            Translate as academic / scholarly prose. Use the target
            language's formal scholarly register. Hedged claims ("might
            suggest", "appears to indicate") stay hedged; assertive
            claims stay assertive. Citations, footnote markers, figure
            references, and technical terminology must land verbatim.
            Latin / Greek / French scholarly idioms (e.g. ibid., cf.,
            i.e., a priori) are preserved unless the target academic
            tradition uses a different convention. Maintain the
            source's argumentative structure paragraph by paragraph.
            """
        ),
    ),
    StyleProfile(
        id="journalistic",
        name="Journalistic / reportage",
        description=(
            "Newsroom register. Compact leads, attributed quotes, neutral tone."
        ),
        prompt_block=_normalize(
            """
            Translate as journalism / reportage. Newsroom register:
            compact leads, attributed quotes preserved verbatim where
            possible, neutral framing. Attributions ("said", "according
            to") use the target language's standard reporting verbs.
            Numbers, dates, place names, and titles match the target
            country's house-style conventions. Do not editorialize
            beyond what the source already does. Headlines, datelines,
            and bylines retain their structural cues.
            """
        ),
    ),
)

PROFILE_REGISTRY: dict[str, StyleProfile] = {p.id: p for p in _PROFILES}
"""Lookup table keyed by profile slug.

Insertion-ordered (Python 3.7+) so iteration in the UI matches the
order above — :data:`DEFAULT_STYLE_PROFILE` first, then literary
neighbours (classic, historical), then audience-graded fiction and
fairytale, then adult-genre flavours, then cross-cutting registers
(humor, memoir), then specialty registers (poetry, religious,
technical, academic, journalistic)."""


def list_profiles() -> tuple[StyleProfile, ...]:
    """Return every shipped profile, in display order."""

    return _PROFILES


def get_profile(profile_id: str) -> StyleProfile | None:
    """Return the profile matching ``profile_id`` or ``None`` if unknown.

    Unknown ids are tolerated rather than raising so callers reading old
    project rows (e.g. after a future profile is renamed) can fall back
    cleanly to the custom-text path or the default.
    """

    return PROFILE_REGISTRY.get(profile_id)


def resolve_style_guide(
    *,
    profile_id: str | None,
    custom_text: str | None = None,
) -> str | None:
    """Compute the actual style-guide text to persist on a project row.

    Resolution order:

    1. ``custom_text`` is used verbatim when truthy (the curator's
       authored prose wins; this also covers the case where they
       picked a preset, edited the prefilled text, and saved).
    2. Otherwise the registered profile's ``prompt_block`` is used.
    3. Returns ``None`` when ``profile_id`` is ``None`` *and* no custom
       text was provided — the translator prompt then omits the style
       block entirely (status quo for projects with no style set).
    """

    if custom_text is not None:
        cleaned = custom_text.strip()
        if cleaned:
            return cleaned
    if profile_id is None:
        return None
    profile = PROFILE_REGISTRY.get(profile_id)
    if profile is None:
        return None
    return profile.prompt_block


def suggest_style_profile(
    *,
    register: str | None,
    audience: str | None,
) -> str | None:
    """Map a helper-LLM ``(register, audience)`` to a profile id.

    The helper LLM (M5 intake) is asked to surface a free-form register
    and audience guess for the book; this function maps the most common
    answers to one of our presets so the dashboard can surface a
    one-click "Apply suggestion" affordance. Returns ``None`` when
    nothing matches — the caller should then fall back to the project's
    current setting (or :data:`DEFAULT_STYLE_PROFILE` for fresh projects).

    The mapping is intentionally conservative: ambiguous or
    unrecognized values produce ``None`` rather than guessing wildly.
    The curator can always pick a profile manually from Settings.
    """

    norm_audience = _normalize_token(audience)
    norm_register = _normalize_token(register)

    if norm_audience in {"children", "kids", "picture", "early_reader"}:
        return "children_picture"
    if norm_audience in {"middle_grade", "middle", "tween"}:
        return "middle_grade"
    if norm_audience in {"young_adult", "ya", "teen", "teenager"}:
        return "young_adult"

    if norm_register in {"explicit", "erotic", "erotica", "adult_explicit"}:
        return "explicit_adult"
    if norm_register in {"technical", "instructional", "how_to", "manual"}:
        return "technical_manual"
    if norm_register == "academic":
        return "academic"
    if norm_register in {"journalistic", "news", "reportage"}:
        return "journalistic"
    if norm_register in {
        "religious",
        "sacred",
        "spiritual",
        "liturgical",
        "devotional",
        "theological",
    }:
        return "religious_spiritual"
    if norm_register in {"poetry", "poetic", "verse", "lyric", "lyrical"}:
        return "poetry_verse"
    if norm_register in {
        "memoir",
        "autobiography",
        "autobiographical",
        "biography",
        "biographical",
    }:
        return "memoir_biography"
    if norm_register in {"horror", "gothic", "supernatural_horror"}:
        return "horror_gothic"
    if norm_register in {"noir", "hardboiled", "hard_boiled"}:
        return "noir_crime"
    if norm_register in {"romantic", "romance", "cozy_romance"}:
        return "cozy_romance"
    if norm_register in {
        "humor",
        "humorous",
        "comedy",
        "comedic",
        "satire",
        "satirical",
    }:
        return "humor_comedy"
    if norm_register in {
        "fairytale",
        "fairy_tale",
        "folklore",
        "folktale",
        "folk_tale",
        "fable",
        "myth",
        "mythology",
        "legend",
    }:
        return "fairytale_folklore"
    if norm_register in {"historical", "period", "historical_fiction"}:
        return "historical_fiction"
    if norm_register in {
        "classic",
        "classical",
        "victorian",
        "edwardian",
        "nineteenth_century",
        "19th_century",
        "early_twentieth_century",
    }:
        return "classic_literature"
    if norm_register in {"genre", "thriller", "fantasy", "sf", "scifi"}:
        return "genre_fiction"

    if norm_audience in {"adult", "general"} and norm_register in {
        "literary",
        "literary_fiction",
        None,
        "neutral",
    }:
        return "literary_fiction"
    return None


def _normalize_token(value: str | None) -> str | None:
    """Lower-case + underscore-collapse one helper LLM token.

    Models tend to return ``"Young Adult"`` or ``"young-adult"`` or
    ``"YA"``; we collapse all of those to a single canonical form so the
    suggester's lookup table stays small.
    """

    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    cleaned = cleaned.replace("-", "_").replace(" ", "_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned


def label_for(profile_id: str | None) -> str:
    """Human-readable label for a (possibly-unknown / custom) profile id.

    Used by the dashboard / settings panel where we want to show "Custom"
    when ``profile_id`` is None but ``style_guide`` text exists, and the
    profile's display name otherwise.
    """

    if profile_id is None:
        return "Custom"
    profile = PROFILE_REGISTRY.get(profile_id)
    if profile is None:
        return f"Unknown ({profile_id})"
    return profile.name


def profile_choices() -> list[tuple[str, str]]:
    """Return ``(id, name)`` tuples suitable for a Textual ``Select``.

    Convenience for the New Project / Settings UIs so they don't have to
    iterate :func:`list_profiles` themselves.
    """

    return [(p.name, p.id) for p in _PROFILES]


def iter_profile_ids() -> Iterable[str]:
    """Iterate every shipped profile id (including the default)."""

    return PROFILE_REGISTRY.keys()


__all__ = [
    "DEFAULT_STYLE_PROFILE",
    "PROFILE_REGISTRY",
    "StyleProfile",
    "get_profile",
    "iter_profile_ids",
    "label_for",
    "list_profiles",
    "profile_choices",
    "resolve_style_guide",
    "suggest_style_profile",
]
