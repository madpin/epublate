"""Per-model pricing table (PRD F-LLM-7).

Every LLM call records ``cost_usd`` in the ``llm_call`` table so the cost
meter, batch budget cap, and audit bundle have first-class numbers to work
with. The table is in-memory and user-editable via :func:`set_price`; M2
seeds it with conservative public defaults for a handful of OpenAI-family
slugs and a free ``mock`` entry for tests.

Costs are stored in **USD per million tokens** because the OpenAI public
price sheet is denominated that way; multiplying by ``tokens / 1_000_000``
keeps the math obvious in the call site.
"""

from __future__ import annotations

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
# zero — see :func:`get_price`.
_DEFAULTS: dict[str, ModelPrice] = {
    "gpt-5-mini": ModelPrice(input_per_mtok=0.15, output_per_mtok=0.60),
    "gpt-4o": ModelPrice(input_per_mtok=2.50, output_per_mtok=10.00),
    "gpt-4-turbo": ModelPrice(input_per_mtok=10.00, output_per_mtok=30.00),
    "gpt-4": ModelPrice(input_per_mtok=30.00, output_per_mtok=60.00),
    "gpt-3.5-turbo": ModelPrice(input_per_mtok=0.50, output_per_mtok=1.50),
    "mock": ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0),
}

_UNKNOWN: ModelPrice = ModelPrice(input_per_mtok=0.0, output_per_mtok=0.0)

_TABLE: dict[str, ModelPrice] = dict(_DEFAULTS)


def get_price(model: str) -> ModelPrice:
    """Lookup the price entry for ``model``; unknown models bill at zero."""

    return _TABLE.get(model, _UNKNOWN)


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
