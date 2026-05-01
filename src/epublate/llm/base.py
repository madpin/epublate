"""LLM provider interface (PRD §6.6).

Every LLM call in epublate goes through :class:`LLMProvider`. v1 ships only
the OpenAI-compatible implementation (M2) and the deterministic mock (M0);
provider-specific code paths are explicitly forbidden by ``AGENTS.md``.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """A single chat-completions message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    content: str


class ResponseFormat(BaseModel):
    """Optional structured-output hint passed through to the endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["text", "json_object", "json_schema"] = "text"
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


class ChatResult(BaseModel):
    """Result of a single chat-completions call.

    ``raw`` retains the unparsed provider payload so the persistence layer can
    record it in ``llm_call.response_json`` (PRD §6.4 / NFR-2 audit trail).
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str
    cache_hit: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal chat-completions surface required by the pipeline."""

    name: str

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        response_format: ResponseFormat | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> ChatResult:
        """Run a single chat-completion request and return the parsed result."""
        ...


__all__ = [
    "ChatResult",
    "LLMProvider",
    "Message",
    "ResponseFormat",
    "Role",
]
