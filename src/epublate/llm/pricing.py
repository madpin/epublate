"""Per-model pricing table (PRD F-LLM-7).

Every LLM call records ``cost_usd`` in the ``llm_call`` table so the cost
meter, batch budget cap, and audit bundle have first-class numbers to work
with. The table is in-memory and user-editable via :func:`set_price`; M2
seeds it with conservative public defaults for a handful of OpenAI-family
slugs and a free ``mock`` entry for tests.

Costs are stored in **USD per million tokens** because the OpenAI public
price sheet is denominated that way; multiplying by ``tokens / 1_000_000``
keeps the math obvious in the call site.

Lookup is forgiving: an exact ``model`` name wins, then we strip the
trailing ``-YYYY-MM-DD`` date stamp that the OpenAI API returns
(``gpt-4o-2024-08-06``), then we try the longest registered prefix
(``gpt-4o-mini-2024-07-18`` → ``gpt-4o-mini``). Local / OSS endpoints
(``llama3:70b``, ``mistral``) miss every fallback and bill at zero,
which is what we want for free providers.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class ModelPrice(BaseModel):
    """USD cost per million prompt / completion tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)

    def cost_for(self, prompt_tokens: int, completion_tokens: int) -> float:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("token counts must be non-negative")
        return (
            prompt_tokens * self.input_per_mtok / 1_000_000.0
            + completion_tokens * self.output_per_mtok / 1_000_000.0
        )


# Public reference prices snapshotted at PRD authoring time. Local / OSS
# endpoints (Ollama, llama.cpp) bill nothing, so unknown models default to
# zero — see :func:`get_price`. Prices are intentionally conservative
# (we'd rather *over*-estimate cost in the meter than report a fake $0).
_DEFAULTS: dict[str, ModelPrice] = {
    # GPT-5 family (preview pricing snapshot).
    "gpt-5": ModelPrice(input_per_mtok=1.25, output_per_mtok=10.00),
    "gpt-5-mini": ModelPrice(input_per_mtok=0.25, output_per_mtok=2.00),
    "gpt-5-nano": ModelPrice(input_per_mtok=0.05, output_per_mtok=0.40),
    # GPT-4 family.
    "gpt-4o": ModelPrice(input_per_mtok=2.50, output_per_mtok=10.00),
    "gpt-4o-mini": ModelPrice(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-4-turbo": ModelPrice(input_per_mtok=10.00, output_per_mtok=30.00),
    "gpt-4.1": ModelPrice(input_per_mtok=2.00, output_per_mtok=8.00),
    "gpt-4.1-mini": ModelPrice(input_per_mtok=0.40, output_per_mtok=1.60),
    "gpt-4.1-nano": ModelPrice(input_per_mtok=0.10, output_per_mtok=0.40),
    "gpt-4": ModelPrice(input_per_mtok=30.00, output_per_mtok=60.00),
    # o-series reasoning models.
    "o1": ModelPrice(input_per_mtok=15.00, output_per_mtok=60.00),
    "o1-mini": ModelPrice(input_per_mtok=1.10, output_per_mtok=4.40),
    "o3": ModelPrice(input_per_mtok=10.00, output_per_mtok=40.00),
    "o3-mini": ModelPrice(input_per_mtok=1.10, output_per_mtok=4.40),
    "o4-mini": ModelPrice(input_per_mtok=1.10, output_per_mtok=4.40),
    # Legacy chat models.
    "gpt-3.5-turbo": ModelPrice(input_per_mtok=0.50, output_per_mtok=1.50),
    # Test sentinel: free, deterministic.
    "mock": ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0),
}

_UNKNOWN: ModelPrice = ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0)

_TABLE: dict[str, ModelPrice] = dict(_DEFAULTS)

# OpenAI tags responses with a ``-YYYY-MM-DD`` snapshot suffix
# (``gpt-4o-2024-08-06``). Strip it before falling back to prefix
# matching so a fresh snapshot still resolves to its family pricing.
_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def get_price(model: str) -> ModelPrice:
    """Lookup the price entry for ``model``; unknown models bill at zero.

    Tries exact match first, then strips a trailing ``-YYYY-MM-DD``
    date stamp, then walks back over hyphen boundaries until a known
    family prefix matches. This lets us price ``gpt-4o-2024-08-06``,
    ``gpt-4o-mini-2024-07-18``, and ``o3-mini-2025-01-31`` without
    having to register every snapshot revision the provider ships.
    """

    if model in _TABLE:
        return _TABLE[model]
    stripped = _DATE_SUFFIX_RE.sub("", model)
    if stripped != model and stripped in _TABLE:
        return _TABLE[stripped]
    parts = stripped.split("-")
    while len(parts) > 1:
        parts.pop()
        prefix = "-".join(parts)
        if prefix in _TABLE:
            return _TABLE[prefix]
    return _UNKNOWN


def set_price(model: str, price: ModelPrice) -> None:
    """Override ``model``'s price entry in the in-memory table."""

    _TABLE[model] = price


def reset_prices() -> None:
    """Restore the package-default price table.

    Useful in tests that mutate the table; production code should not need
    this.
    """

    _TABLE.clear()
    _TABLE.update(_DEFAULTS)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost for a chat-completion at ``model``'s current rate."""

    return get_price(model).cost_for(prompt_tokens, completion_tokens)


__all__ = [
    "ModelPrice",
    "estimate_cost",
    "get_price",
    "reset_prices",
    "set_price",
]
