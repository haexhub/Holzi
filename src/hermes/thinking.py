"""Per-model thinking / reasoning capability and wire-format dispatch.

Two concerns kept in one place:

1. **Capability** — does a given model support extended thinking, and at
   which levels? Used by `/api/models` so the composer can hide / show
   the picker.
2. **Wire format** — how do we express the chosen budget on the request
   body? Each provider speaks a different dialect:

   - Anthropic: `{"thinking": {"type":"enabled","budget_tokens": N}}`
     plus the constraint `max_tokens > budget_tokens`.
   - OpenAI: `{"reasoning_effort": "low"|"medium"|"high"}`.
   - OpenRouter: `{"reasoning": {"effort": "..."}}`.

Hybrid capability source: trust provider metadata when available
(OpenRouter's `supported_parameters`), curated rules otherwise. Bump the
rules when new model families ship.
"""
import re
from dataclasses import dataclass
from typing import Any, Literal

ThinkingBudget = Literal["low", "medium", "high"]

# Anthropic budget_tokens per level. The numbers are conservative
# defaults — high < 32k (Anthropic's cap on most current models).
_ANTHROPIC_BUDGET_TOKENS: dict[ThinkingBudget, int] = {
    "low": 1024,
    "medium": 5000,
    "high": 16000,
}

# Safety margin added to budget_tokens to produce max_tokens. Anthropic
# requires max_tokens > budget_tokens; the model still needs headroom
# for the visible answer itself after the thinking block.
_ANTHROPIC_MAX_TOKENS_OVER_BUDGET = 4096

_LEVELS: tuple[str, ...] = ("low", "medium", "high")


# ─── capability rules ────────────────────────────────────────────────


# Per provider: regex patterns matched against the model id. First list
# is "supports thinking", second is "definitely does not" — the second
# list exists only to keep the intent legible; falling through both
# lists means "unsupported".
_RULES: dict[str, tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]] = {
    "anthropic": (
        (
            re.compile(r"^claude-3-7-sonnet"),
            re.compile(r"^claude-(opus|sonnet|haiku)-4"),
            re.compile(r"^claude-(opus|sonnet|haiku)-[5-9]"),
        ),
        (
            re.compile(r"^claude-3-5"),
            re.compile(r"^claude-3-haiku"),
            re.compile(r"^claude-3-opus"),
        ),
    ),
    "openai": (
        (
            re.compile(r"^o[134](-|$)"),
            re.compile(r"^gpt-5"),
        ),
        (
            re.compile(r"^gpt-4"),
            re.compile(r"^gpt-3\.5"),
        ),
    ),
    "google": (
        (
            re.compile(r"^gemini-2\.5"),
            re.compile(r"^gemini-2\.0-flash-thinking"),
        ),
        (
            re.compile(r"^gemini-1"),
            re.compile(r"^gemini-2\.0-(?!flash-thinking)"),
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ThinkingSupport:
    """Capability descriptor for a single model.

    `levels` is empty when `supported` is False, otherwise the budgets
    the composer should offer (currently always low/medium/high — kept
    as a tuple so future per-provider variation is non-breaking)."""

    supported: bool
    levels: tuple[str, ...]


_SUPPORTED = ThinkingSupport(supported=True, levels=_LEVELS)
_UNSUPPORTED = ThinkingSupport(supported=False, levels=())


def resolve_thinking_support(
    provider: str,
    model_id: str,
    supported_parameters: list[str] | None = None,
) -> ThinkingSupport:
    """Return whether `model_id` on `provider` supports extended thinking.

    `supported_parameters` is OpenRouter's per-model metadata
    (`["tools","reasoning",...]`); when present it wins over the curated
    rules. For other providers it's `None` and rules apply."""
    if supported_parameters is not None:
        return _SUPPORTED if "reasoning" in supported_parameters else _UNSUPPORTED

    rules = _RULES.get(provider)
    if rules is None:
        return _UNSUPPORTED
    supports, _excludes = rules
    for pattern in supports:
        if pattern.search(model_id):
            return _SUPPORTED
    return _UNSUPPORTED


# ─── wire-format dispatch ────────────────────────────────────────────


def build_thinking_payload(
    provider: str,
    model_id: str,
    budget: ThinkingBudget,
    *,
    supported_parameters: list[str] | None = None,
) -> dict[str, Any]:
    """Return a body fragment to merge into the upstream request.

    Empty dict when the model / provider can't express the budget —
    callers should always be able to `body.update(...)` the result
    safely. A `logger`-level note is the caller's job; this stays pure."""
    support = resolve_thinking_support(provider, model_id, supported_parameters)
    if not support.supported:
        return {}

    if provider == "anthropic":
        budget_tokens = _ANTHROPIC_BUDGET_TOKENS[budget]
        return {
            "thinking": {"type": "enabled", "budget_tokens": budget_tokens},
            "max_tokens": budget_tokens + _ANTHROPIC_MAX_TOKENS_OVER_BUDGET,
        }
    if provider == "openai":
        return {"reasoning_effort": budget}
    if provider == "openrouter":
        return {"reasoning": {"effort": budget}}

    # google chat path isn't wired upstream yet — leave the body alone
    # rather than guessing at a translation that may not match the
    # eventual upstream shape.
    return {}
