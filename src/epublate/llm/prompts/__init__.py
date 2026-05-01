"""Prompt templates and response parsers (PRD §8)."""

from epublate.llm.prompts.extractor import (
    ExtractedEntity,
    ExtractorTrace,
    build_extractor_messages,
    parse_extractor_response,
)
from epublate.llm.prompts.translator import (
    GlossaryConstraint,
    TranslatorTrace,
    build_translator_messages,
    parse_translator_response,
)

__all__ = [
    "ExtractedEntity",
    "ExtractorTrace",
    "GlossaryConstraint",
    "TranslatorTrace",
    "build_extractor_messages",
    "build_translator_messages",
    "parse_extractor_response",
    "parse_translator_response",
]
