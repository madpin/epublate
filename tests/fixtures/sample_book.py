"""Source-of-truth prose for the CC0 ``sample.epub`` test fixture.

The text is original and dedicated to the public domain via CC0; see
``tests/fixtures/NOTICE.md``. Keep it small (the round-trip suite reads
this fixture on every CI run) and bilingual-test-friendly: include
inline emphasis, an anchor, and at least one heading per chapter so the
ePub adapter and validator both see real placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixtureChapter:
    uid: str
    file_name: str
    title: str
    body_xhtml: str


TITLE = "The Lighthouse Keeper's Ledger"
AUTHOR = "epublate test contributors"
LANGUAGE = "en"
IDENTIFIER = "urn:epublate:fixture:sample"

CHAPTERS: tuple[FixtureChapter, ...] = (
    FixtureChapter(
        uid="ch01",
        file_name="ch01.xhtml",
        title="On the First Lamp",
        body_xhtml=(
            "<h1>On the First Lamp</h1>"
            "<p>The keeper called it the <em>first lamp</em> because it "
            "burned before the others, long before the harbor was named.</p>"
            "<p>Each evening, when the gulls turned home, she climbed the "
            "spiral stair and lit the wick. The flame was small, but "
            "<strong>steadfast</strong>, and the sea respected it.</p>"
        ),
    ),
    FixtureChapter(
        uid="ch02",
        file_name="ch02.xhtml",
        title="Notes on the Tide",
        body_xhtml=(
            "<h1>Notes on the Tide</h1>"
            "<p>She kept her ledger in a tin box beside the lamp, and she "
            "wrote in it whenever a ship passed in the dark.</p>"
            "<p>One night she recorded a strange vessel with no flag, "
            'moving against the current. See <a href="ch03.xhtml#fn1">'
            "footnote</a> for the captain's name, which she could not "
            "spell.</p>"
        ),
    ),
    FixtureChapter(
        uid="ch03",
        file_name="ch03.xhtml",
        title="The Footnote",
        body_xhtml=(
            "<h1>The Footnote</h1>"
            "<p>Years later, a scholar visited the lighthouse and read the "
            "ledger by candlelight. <em>The captain's name</em>, she "
            "decided, must have been written in a language the keeper "
            "did not know.</p>"
            '<p id="fn1">She copied it carefully, and folded the page '
            "into her notebook.</p>"
        ),
    ),
)
