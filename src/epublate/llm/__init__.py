"""LLM provider interface and v1 implementations.

The mock provider ships in M0 (PRD NFR-7). The OpenAI-compatible HTTP client
and the env-driven factory land in M2 per PRD §10.
"""

from epublate.llm.base import ChatResult, LLMProvider, Message, ResponseFormat
from epublate.llm.factory import build_provider
from epublate.llm.mock import MockLLMProvider
from epublate.llm.openai_compat import OpenAICompatProvider, RetryPolicy

__all__ = [
    "ChatResult",
    "LLMProvider",
    "Message",
    "MockLLMProvider",
    "OpenAICompatProvider",
    "ResponseFormat",
    "RetryPolicy",
    "build_provider",
]
