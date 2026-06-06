"""Unit tests for hermes.thinking — pure library, no network."""
import pytest

from hermes.thinking import build_thinking_payload, resolve_thinking_support


@pytest.mark.parametrize(
    ("provider", "model", "supported_params", "expected"),
    [
        ("anthropic", "claude-opus-4-7", None, True),
        ("anthropic", "claude-sonnet-4-6", None, True),
        ("anthropic", "claude-haiku-4-5", None, True),
        ("anthropic", "claude-3-7-sonnet-20250219", None, True),
        ("anthropic", "claude-3-5-sonnet-20241022", None, False),
        ("anthropic", "claude-3-opus-20240229", None, False),
        ("anthropic", "claude-3-haiku-20240307", None, False),
        ("openai", "o1-mini", None, True),
        ("openai", "o3", None, True),
        ("openai", "o4-mini", None, True),
        ("openai", "gpt-5-pro", None, True),
        ("openai", "gpt-4o", None, False),
        ("openai", "gpt-4-turbo", None, False),
        ("openai", "gpt-3.5-turbo", None, False),
        ("google", "gemini-2.5-pro", None, True),
        ("google", "gemini-2.0-flash-thinking-exp", None, True),
        ("google", "gemini-2.0-flash", None, False),
        ("google", "gemini-1.5-pro", None, False),
        # OpenRouter: metadata wins
        ("openrouter", "anthropic/claude-x", ["tools", "reasoning"], True),
        ("openrouter", "anthropic/claude-x", ["tools"], False),
        ("openrouter", "anthropic/claude-x", None, False),  # no rules, no metadata
        # Unknown provider — always unsupported
        ("mystery", "whatever", None, False),
    ],
)
def test_resolve_thinking_support(provider, model, supported_params, expected):
    result = resolve_thinking_support(provider, model, supported_params)
    assert result.supported is expected
    assert result.levels == (("low", "medium", "high") if expected else ())


def test_anthropic_payload_high():
    payload = build_thinking_payload("anthropic", "claude-opus-4-7", "high")
    assert payload == {
        "thinking": {"type": "enabled", "budget_tokens": 16000},
        "max_tokens": 16000 + 4096,
    }


def test_anthropic_payload_low():
    payload = build_thinking_payload("anthropic", "claude-sonnet-4-6", "low")
    assert payload["thinking"]["budget_tokens"] == 1024
    assert payload["max_tokens"] > payload["thinking"]["budget_tokens"]


def test_openai_payload_medium():
    payload = build_thinking_payload("openai", "o1-mini", "medium")
    assert payload == {"reasoning_effort": "medium"}


def test_openrouter_payload_low():
    payload = build_thinking_payload(
        "openrouter", "anthropic/claude-x", "low",
        supported_parameters=["reasoning"],
    )
    assert payload == {"reasoning": {"effort": "low"}}


def test_unsupported_model_returns_empty():
    assert build_thinking_payload("openai", "gpt-4o", "high") == {}


def test_unsupported_anthropic_model_returns_empty():
    assert build_thinking_payload("anthropic", "claude-3-5-sonnet-latest", "high") == {}


def test_google_returns_empty_until_chat_path_lands():
    # Google chat path isn't live — we return {} even for known-thinking models
    # rather than guess at the upstream shape.
    assert build_thinking_payload("google", "gemini-2.5-pro", "high") == {}


def test_unknown_provider_returns_empty():
    assert build_thinking_payload("mystery", "whatever", "high") == {}
