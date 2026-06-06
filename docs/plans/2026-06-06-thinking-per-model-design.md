# Per-Model Thinking Support

Status: design approved 2026-06-06, ready to implement.

## Problem

`thinking_budget: low|medium|high` on `POST /api/chat` currently injects an
Anthropic-native `thinking` block unconditionally in [agent.py:166-170](../../src/hermes/agent.py#L166-L170):

```python
body["thinking"] = {"type": "enabled", "budget_tokens": N}
```

Two issues:

1. **Wire format is Anthropic-only.** The agent speaks OpenAI-compatible
   `/v1/chat/completions`. For Anthropic the haex-claude-proxy translates,
   but a direct OpenAI / OpenRouter credential silently drops the field —
   the user sets "high" thinking and gets none.
2. **Capability not surfaced.** The composer offers low/med/high for
   every model, including those that have no thinking mode at all
   (gpt-4o, claude-3-5-sonnet, gemini-1.5-flash).

Goal: backend sends the provider-correct field for the credential's
provider, and `/api/models` tells the UI which models support thinking
so the composer can adapt.

## Capability source — hybrid

- **Provider metadata when available.** OpenRouter's `/v1/models` items
  carry `supported_parameters` including `"reasoning"`. Use it.
- **Curated regex/prefix rules otherwise.** OpenAI / Anthropic / Google
  `/v1/models` don't reliably expose reasoning capability, so we ship a
  table keyed by `(provider, pattern)`. Bump the table when new model
  families ship — same pattern as the existing
  `ANTHROPIC_OAUTH_MODELS` curated list in [provider_models.py](../../src/hermes/provider_models.py).

## Architecture

### New module: `src/hermes/thinking.py`

Two pure functions, no I/O. Lives separately from `provider_models.py`
because `provider_models.py` is about *listing* models, this is about
their *capabilities and wire format*.

```python
@dataclass(frozen=True, slots=True)
class ThinkingSupport:
    supported: bool
    levels: tuple[str, ...]   # ("low","medium","high") or ()

def resolve_thinking_support(
    provider: str,
    model_id: str,
    supported_parameters: list[str] | None = None,
) -> ThinkingSupport:
    """Hybrid: prefer supported_parameters when present (OpenRouter),
    fall back to curated rules per provider."""

def build_thinking_payload(
    provider: str,
    model_id: str,
    budget: Literal["low", "medium", "high"],
) -> dict[str, Any]:
    """Body fragment to merge into the request.

    - anthropic  -> {"thinking": {"type":"enabled","budget_tokens":N},
                     "max_tokens": N + 4096}
    - openai     -> {"reasoning_effort": budget}
    - openrouter -> {"reasoning": {"effort": budget}}
    - google     -> {} + debug log (no chat path live yet)
    - unsupported model -> {} + debug log
    """
```

`_THINKING_TOKENS = {"low":1024, "medium":5000, "high":16000}` moves here
from `agent.py`.

### Curated rules (initial table)

| provider   | supports thinking                                  | excludes                       |
|------------|----------------------------------------------------|--------------------------------|
| anthropic  | `claude-3-7-sonnet*`, `*-opus-4*`, `*-sonnet-4*`, `*-haiku-4*` | `claude-3-5-*`, `*-haiku-3*`, `claude-3-opus-*` |
| openai     | `o1*`, `o3*`, `o4*`, `gpt-5*`                      | `gpt-4*`, `gpt-3.5*`           |
| google     | `gemini-2.5*`, `gemini-2.0-flash-thinking*`        | other gemini                   |
| openrouter | metadata-driven (`supported_parameters`)           | metadata-driven                |

### `/api/models` — capability surfacing

`ModelEntry` gains two fields:

```python
class ThinkingSupportDTO(BaseModel):
    supported: bool
    levels: list[str]

class ModelEntry(BaseModel):
    id: str
    credential_id: int
    credential_name: str
    provider: str                           # NEW
    thinking: ThinkingSupportDTO            # NEW
```

`api_models` is refactored to call `list_provider_models()` instead of
hitting `/v1/models` itself. Reasons:

- That function already caches per `(provider, cred_id)` for 10 min.
- For OpenRouter it has access to `supported_parameters` (currently
  thrown away — needs a small change to keep raw items reachable).
- Eliminates the duplicate `try/except` around the raw GET that we
  patched last commit.

When `list_provider_models` raises `ProviderModelsError`, fall back to
`cred.model` and run `resolve_thinking_support(provider, cred.model,
None)` so the picker still has a working entry.

To preserve OpenRouter's `supported_parameters` for capability
resolution, `provider_models.py` needs a richer return shape. Smallest
change: add an optional `supported_parameters: tuple[str, ...] | None`
field to `ModelChoice`. Populated only by `_list_openai_like` when the
provider is `openrouter`. Other listers leave it `None`. No behavior
change for existing callers.

### Wire dispatch in `run_agent`

`run_agent` already receives `model` and `thinking_budget`. Add
`provider: str` (passed from `_stream_web_agent_run` as
`persona_ctx.credential.provider`).

Replace [agent.py:166-170](../../src/hermes/agent.py#L166-L170):

```python
if thinking_budget is not None:
    body.update(build_thinking_payload(provider, model, thinking_budget))
```

`build_thinking_payload` returns `{}` for unsupported models / providers
without a wire format, so the call is safe even when the composer
optimistically sends a budget.

### No server-side capability validation

`ChatRequest.thinking_budget: Literal[...]` already gives a clean 400 on
invalid string values via Pydantic. We deliberately do **not** reject a
budget for a model that can't use it — the composer drives picker
options from `/api/models`, double-validation is duplication, and the
user can legitimately switch model between picker-click and POST.
Unsupported budgets are silently dropped with a debug log.

## File changes summary

- **new** `src/hermes/thinking.py` — pure capability + wire library.
- **edit** `src/hermes/agent.py` — `run_agent` takes `provider`, calls
  `build_thinking_payload`. `_THINKING_TOKENS` removed (moved to
  thinking.py).
- **edit** `src/hermes/routes/api.py` —
  - `ModelEntry` gains `provider` + `thinking`.
  - `api_models` migrated to `list_provider_models`.
  - `_stream_web_agent_run` reaches `persona_ctx.credential.provider`
    into `run_agent`.
- **edit** `src/hermes/provider_models.py` — `ModelChoice.supported_parameters`
  optional field, populated by OpenRouter lister only.

## Tests

### `tests/test_thinking.py` — new

Pure unit tests, no network:

- `resolve_thinking_support`:
  - `("anthropic","claude-opus-4-7")` → supported, 3 levels
  - `("anthropic","claude-3-5-sonnet-20241022")` → unsupported
  - `("openai","o1-mini")` → supported
  - `("openai","gpt-4o")` → unsupported
  - `("openai","gpt-5-pro")` → supported
  - `("google","gemini-2.5-pro")` → supported
  - `("google","gemini-1.5-flash")` → unsupported
  - `("openrouter","x/y", ["reasoning","tools"])` → supported
  - `("openrouter","x/y", ["tools"])` → unsupported
  - `("openrouter","x/y", None)` → unsupported (no metadata, no rules)
  - `("mystery","whatever")` → unsupported

- `build_thinking_payload`:
  - anthropic + high → `thinking.budget_tokens=16000`, `max_tokens=20096`
  - openai supported model + medium → `{"reasoning_effort":"medium"}`
  - openrouter supported + low → `{"reasoning":{"effort":"low"}}`
  - unsupported model → `{}`
  - unknown provider → `{}`

### `tests/test_api_thinking_budget.py` — extend

Existing 3 tests cover anthropic path (default seeded cred). Add:

- OpenAI credential + `o1-mini` model_override + budget medium →
  mock upstream sees `reasoning_effort: "medium"`, no `thinking` field.
- OpenAI credential + `gpt-4o` + budget high → mock upstream sees
  neither `thinking` nor `reasoning_effort`.

Implementation note: the existing tests seed an Anthropic credential via
the autouse fixture. For these new cases we need to swap the active
credential — easiest path: monkeypatch `persona_ctx` or seed via the
existing credential seeding helper.

### `tests/test_api_models.py` — extend

`test_models_returns_list`: assert every entry has `provider: str` and
`thinking: {supported: bool, levels: list[str]}`. Shape-only — concrete
values depend on the seeded credential.

## Out of scope

- Google Gemini direct chat path (no upstream wired up today).
- MCP tool-use interaction with thinking (orthogonal, already works).
- Reasoning streaming format differences across providers (orthogonal,
  already handled in `_request_round_stream`).
- Per-model default budget (composer remembers user choice).
