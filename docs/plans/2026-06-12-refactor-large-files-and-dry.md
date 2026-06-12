# Refactor Large Files (>500 LoC) + DRY Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split every production and test file >500 LoC into focused modules, and abstract real DRY violations into shared helpers. Behavior must be unchanged — refactor only.

**Architecture:**
- Production files split along **route-group** (api.py), **operation-class** (workspace.py: browser/writer/git), or **data-vs-logic** (personas.py, starter_skills.py) boundaries.
- Cross-route helpers consolidated into `src/hermes/routes/_helpers.py` and `src/hermes/sandbox/exec_util.py`.
- Test files split along the **same boundaries as their production counterparts**; common fixtures promoted to `tests/conftest.py`, common helpers to `tests/_helpers.py`.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy Core (async), pytest, pytest-asyncio.

**Hard Rules:**
- Every commit MUST leave `pytest` green and `ruff check` + `mypy` (or whatever the repo gates on) clean.
- No behavior changes. No API surface changes. No deletions of code that isn't moved elsewhere.
- After each task, re-run the affected test file(s) to confirm green.
- Commit after each task — small, atomic commits.

**Prerequisite:** PR #87 (§1 Postgres + RLS) is merged to `main` before starting Phase 1. Until then, only Phase 0 (this plan + a branch scaffold) is safe.

---

## Phase 0: Preparation (do once)

### Task 0.1: Confirm PR #87 merged, create refactor branch

**Steps:**
1. `gh pr view 87 --json state` → confirm `"MERGED"`. If not merged, STOP and wait.
2. `git checkout main && git pull --ff-only`
3. `git checkout -b refactor/split-large-files-and-dry`
4. Run full test suite once to capture baseline: `pytest -q | tee /tmp/baseline-tests.txt`
5. Expected: all tests green. If anything fails, STOP — fix on main first.
6. Run line-count sanity check: `find src/hermes tests -name "*.py" -exec wc -l {} + | sort -rn | head -20 | tee /tmp/baseline-loc.txt`
7. Commit nothing yet.

---

## Phase 1: Shared helpers (DRY foundation)

Order matters — extract helpers BEFORE splits so the splits can import them.

### Task 1.1: Create `src/hermes/routes/_helpers.py` with `_require_sandbox_manager` + `_validate_limit`

**Files:**
- Create: `src/hermes/routes/_helpers.py`
- Modify: `src/hermes/routes/api.py:1804` (remove local `_require_sandbox_manager`)
- Modify: `src/hermes/routes/workspace.py:290` (remove local `_require_sandbox_manager`)
- Modify: `src/hermes/routes/api.py:87` (move `_validate_limit` out)

**Step 1: Write the failing test**

```python
# tests/test_routes_helpers.py
import pytest
from fastapi import HTTPException
from hermes.routes._helpers import require_sandbox_manager, validate_limit


def test_require_sandbox_manager_returns_manager_from_app_state():
    class FakeMgr: pass
    class FakeReq:
        class app:
            class state:
                sandbox_manager = FakeMgr()
    req = FakeReq()
    assert require_sandbox_manager(req) is req.app.state.sandbox_manager


def test_require_sandbox_manager_raises_503_when_missing():
    class FakeReq:
        class app:
            class state:
                pass
    with pytest.raises(HTTPException) as exc:
        require_sandbox_manager(FakeReq())
    assert exc.value.status_code == 503


def test_validate_limit_clamps_to_max():
    from hermes.routes._helpers import validate_limit
    assert validate_limit(10, max_limit=5) == 5


def test_validate_limit_floors_at_one():
    from hermes.routes._helpers import validate_limit
    assert validate_limit(0) == 1
    assert validate_limit(-5) == 1
```

**Step 2: Run, expect ImportError / module not found**
`pytest tests/test_routes_helpers.py -v` → FAIL

**Step 3: Implement `_helpers.py`** — copy the existing implementations from `api.py:87` and `workspace.py:290` (the function bodies are already correct; just drop the leading `_` to make them public).

**Step 4: Run, expect PASS**
`pytest tests/test_routes_helpers.py -v` → PASS

**Step 5: Update call sites**
- In `api.py`: delete the `_require_sandbox_manager` definition, delete the `_validate_limit` definition, add `from hermes.routes._helpers import require_sandbox_manager, validate_limit`. Replace internal calls.
- In `workspace.py`: delete `_require_sandbox_manager`, import the public name. Replace internal calls.

**Step 6: Run affected tests**
`pytest tests/test_api_chat.py tests/test_api_workspace.py tests/test_api_workspace_git.py -q` → all PASS

**Step 7: Commit**
```bash
git add src/hermes/routes/_helpers.py src/hermes/routes/api.py src/hermes/routes/workspace.py tests/test_routes_helpers.py
git commit -m "refactor: extract require_sandbox_manager + validate_limit to routes/_helpers"
```

---

### Task 1.2: Extract `_drain_exec` + `_decode` to `src/hermes/sandbox/exec_util.py`

**Files:**
- Create: `src/hermes/sandbox/exec_util.py`
- Modify: `src/hermes/routes/workspace.py:379-408` (remove `_drain_exec`)
- Modify: `src/hermes/routes/workspace.py:1187` (remove `_decode`)
- Modify: `src/hermes/routes/workspaces.py:132-173` (remove duplicate `_drain_exec`, import shared)
- Modify: `src/hermes/routes/workspaces.py:195, 228` (replace inline `.decode("utf-8", "replace")` with `decode_bytes`)

**Step 1: Write failing tests**

```python
# tests/test_sandbox_exec_util.py
from hermes.sandbox.exec_util import decode_bytes

def test_decode_bytes_handles_utf8():
    assert decode_bytes(b"hello") == "hello"

def test_decode_bytes_replaces_invalid():
    out = decode_bytes(b"\xff\xfe invalid")
    assert "invalid" in out
    assert "�" in out or out  # uses 'replace' error handler
```

(For `drain_exec`, leave it covered by existing `test_sandbox.py` + `test_api_workspace_git.py` — extraction is mechanical.)

**Step 2: Run** → ImportError

**Step 3: Implement** — copy `_drain_exec` and `_decode` from workspace.py verbatim into exec_util.py. Make both public (drop the underscore).

**Step 4: Run** → PASS

**Step 5: Update call sites in `workspace.py` and `workspaces.py`** to import the shared versions. Delete the local copies.

**Step 6: Run regression tests**
`pytest tests/test_api_workspace.py tests/test_api_workspace_git.py tests/test_sandbox.py -q` → all PASS

**Step 7: Commit**
```bash
git add src/hermes/sandbox/exec_util.py src/hermes/routes/workspace.py src/hermes/routes/workspaces.py tests/test_sandbox_exec_util.py
git commit -m "refactor: extract drain_exec + decode_bytes to sandbox/exec_util"
```

---

### Task 1.3: Add `raise_error()` helper for HTTPException + ErrorCode boilerplate

**Files:**
- Modify: `src/hermes/routes/_helpers.py` (add `raise_error`)
- Modify: callers — start with `src/hermes/routes/preferences.py` only (don't sweep yet — risky)

**Why limited:** The 203 raise-sites span all routes. A blast-radius sweep risks subtle wording changes that break clients. Do preferences.py as proof, then evaluate.

**Step 1: Write failing test**

```python
# tests/test_routes_helpers.py — add this
import pytest
from fastapi import HTTPException
from hermes.routes._helpers import raise_error
from hermes.errors import ErrorCode  # or wherever ErrorCode lives

def test_raise_error_builds_detail_shape():
    with pytest.raises(HTTPException) as exc:
        raise_error(404, ErrorCode.NOT_FOUND, params={"id": 1})
    assert exc.value.status_code == 404
    assert exc.value.detail == {"code": ErrorCode.NOT_FOUND.value, "params": {"id": 1}}

def test_raise_error_omits_params_when_none():
    with pytest.raises(HTTPException) as exc:
        raise_error(400, ErrorCode.BAD_REQUEST)
    assert exc.value.detail == {"code": ErrorCode.BAD_REQUEST.value, "params": {}}
```

**Step 2: Run** → FAIL

**Step 3: Implement `raise_error`** (4-line function in `_helpers.py`).

**Step 4: Run** → PASS

**Step 5: Replace all `raise HTTPException(...)` sites in `preferences.py` only** with `raise_error(...)`. Manual review each one — the wording inside `params` must NOT change.

**Step 6: Run preferences tests**
`pytest tests/test_api_preferences.py -q` → PASS

**Step 7: Commit**
```bash
git add src/hermes/routes/_helpers.py src/hermes/routes/preferences.py tests/test_routes_helpers.py
git commit -m "refactor: introduce raise_error helper, apply in preferences.py as proof"
```

**Decision Gate after Task 1.3:** Inspect the diff. If `raise_error` reads cleanly and preferences.py is meaningfully shorter, schedule a follow-up to sweep the remaining routes. If the helper feels awkward (e.g. params handling is too varied), STOP and document why in the commit message; do not sweep.

---

### Task 1.4: Promote `client`, `configure_workspaces`, `install_sandbox` fixtures to conftest

**Files:**
- Modify: `tests/conftest.py` (add three fixtures)
- Modify: `tests/test_api_chat.py:95-104` (delete local `client`)
- Modify: `tests/test_api_workspace.py:29-82` (delete `client`, `configure_workspaces`, `install_sandbox`)
- Modify: `tests/test_api_workspace_git.py:31-87` (delete `client`, `configure_workspaces`, `install_sandbox`)
- Modify: `tests/test_api_preferences.py:26-35` (delete `client`)
- Modify: `tests/test_api_mcp_servers.py` (delete `client` if present)

**Step 1: Verify the duplicated fixtures are byte-identical** (or differ only in trivial ways)

`diff <(sed -n '95,104p' tests/test_api_chat.py) <(sed -n '29,38p' tests/test_api_workspace.py)`

If they diverge meaningfully, keep them local and instead reconcile to one canonical version before promoting.

**Step 2: Copy the canonical version into `conftest.py`** as session-scoped or function-scoped (match current scope).

**Step 3: Delete the duplicated fixtures from each test file** one at a time.

**Step 4: After EACH file's local fixture is deleted, run that file's tests**
`pytest tests/test_api_chat.py -q` → PASS
Repeat for each.

**Step 5: Run full suite**
`pytest -q` → PASS, no test-count drop vs baseline.

**Step 6: Commit**
```bash
git add tests/conftest.py tests/test_api_chat.py tests/test_api_workspace.py tests/test_api_workspace_git.py tests/test_api_preferences.py tests/test_api_mcp_servers.py
git commit -m "refactor(tests): promote client/configure_workspaces/install_sandbox to conftest"
```

---

### Task 1.5: Extract shared SSE/upstream helpers to `tests/_helpers.py`

**Files:**
- Create: `tests/_helpers.py`
- Modify: `tests/test_api_chat.py:31-92` (delete helpers)
- Modify: `tests/test_approvals.py:42-115` (delete helpers)

**Step 1: Verify duplication** — diff `_install_upstream_responses`, `_to_sse_stream`, `_assistant_oneshot`, `_tool_call_first_response` between the two files.

**Step 2: Copy canonical versions to `tests/_helpers.py`** with public names (drop underscores).

**Step 3: Replace usages in both files** with imports from `tests._helpers`.

**Step 4: Run**
`pytest tests/test_api_chat.py tests/test_approvals.py -q` → PASS

**Step 5: Commit**
```bash
git add tests/_helpers.py tests/test_api_chat.py tests/test_approvals.py
git commit -m "refactor(tests): extract SSE/upstream mock helpers to tests/_helpers"
```

---

## Phase 2: Production-code splits

### Task 2.1: Split `workspace.py` (1701 LoC) into browser / writer / git

**Strategy:** workspace.py first because it has the clearest domain boundary (file ops vs git ops) and the helpers are already extracted.

**Files:**
- Create: `src/hermes/routes/workspace/__init__.py` — re-export router; nothing else.
- Create: `src/hermes/routes/workspace/_models.py` — all Pydantic models (lines 75-275)
- Create: `src/hermes/routes/workspace/_internal.py` — local helpers (lines 279-525, minus already-extracted ones)
- Create: `src/hermes/routes/workspace/browser.py` — `/roots`, `/tree`, `/file` GET (lines 529-777)
- Create: `src/hermes/routes/workspace/writer.py` — `/file` POST/PUT/DELETE, `/rename` (lines 782-1085)
- Create: `src/hermes/routes/workspace/git.py` — all `/git/*` routes (lines 1087-1702)
- Delete: `src/hermes/routes/workspace.py` (at the very end, after all imports updated)
- Modify: `src/hermes/main.py` — import path updates if needed (e.g. `from hermes.routes.workspace import router`)

**Step 1: Extract `_models.py` first** (lowest risk)
1. Create `src/hermes/routes/workspace/__init__.py` (empty for now).
2. Create `src/hermes/routes/workspace/_models.py` and move all `class *(BaseModel)` definitions from workspace.py lines 75-275.
3. In workspace.py, replace those definitions with `from hermes.routes.workspace._models import *` (or list explicitly).
4. Run `pytest tests/test_api_workspace.py tests/test_api_workspace_git.py -q` → PASS.
5. Commit: `refactor(workspace): extract Pydantic models to workspace/_models`

**Step 2: Extract `_internal.py`**
1. Move private helpers (`_active_root_slugs`, `_normalise_relative`, `_absolute_in_sandbox`, `_classify_preview`, `_stat_entry`, `_looks_like_text`, `_resolve_git_workspace`, `_require_repo`, `_working_tree_dirty`, `_normalise_paths`, `_is_git_repo`, `_git_commit`, `_ensure_parent_dir`, `_reject_binary_content`) to `workspace/_internal.py`.
2. In workspace.py, import them back.
3. Run tests → PASS.
4. Commit: `refactor(workspace): extract internal helpers to workspace/_internal`

**Step 3: Extract `browser.py`**
1. Create `workspace/browser.py` with its own `APIRouter`.
2. Move `/roots`, `/tree`, `/file` GET endpoints. Mount the sub-router into the package `__init__.py`'s aggregate router.
3. Delete the moved endpoints from workspace.py.
4. Run tests → PASS.
5. Commit: `refactor(workspace): split browser endpoints into workspace/browser`

**Step 4: Extract `writer.py`** — same pattern as browser.
1. Commit: `refactor(workspace): split writer endpoints into workspace/writer`

**Step 5: Extract `git.py`** — same pattern.
1. Commit: `refactor(workspace): split git endpoints into workspace/git`

**Step 6: Delete workspace.py shell** — at this point it should only contain a router-aggregate import. Move that to `workspace/__init__.py` and delete workspace.py.
1. Update `main.py` import if needed: `from hermes.routes.workspace import router` (this already works because it's a package now).
2. Run full suite → PASS.
3. Commit: `refactor(workspace): remove workspace.py shell, package complete`

**Verification after Task 2.1:**
- `wc -l src/hermes/routes/workspace/*.py` — no file should exceed 500 LoC. If git.py is still ~600, split it further into git_inspect.py (status/diff/log/branches) and git_mutate.py (checkout/stage/commit/push/pull/fetch).

---

### Task 2.2: Split `api.py` (1849 LoC) into sub-route modules

**Files:**
- Create: `src/hermes/routes/api/__init__.py`
- Create: `src/hermes/routes/api/_models.py` — shared Pydantic models that cross route groups
- Create: `src/hermes/routes/api/chat.py` — lines 113-712 (chat, cancel, context, models endpoints)
- Create: `src/hermes/routes/api/approvals.py` — lines 720-869
- Create: `src/hermes/routes/api/runs.py` — lines 877-947
- Create: `src/hermes/routes/api/conversations.py` — lines 954-1426
- Create: `src/hermes/routes/api/attachments.py` — lines 1199-1256 (split out of conversations if it makes conversations >500 LoC)
- Create: `src/hermes/routes/api/notes.py` — lines 1427-1530
- Create: `src/hermes/routes/api/tasks.py` — lines 1537-1789
- Create: `src/hermes/routes/api/sandbox.py` — lines 1798-1849
- Delete: `src/hermes/routes/api.py`

**Execute via same step-by-step pattern as Task 2.1:** one split per commit, run tests after each, names are mechanical.

**Recommended order** (lowest-risk first):
1. `_models.py` — extract shared models (`ChatRequest`, etc.)
2. `notes.py` (smallest, fully independent)
3. `runs.py` (read-only diagnostics)
4. `sandbox.py` (small status endpoint)
5. `tasks.py` (self-contained scheduler)
6. `approvals.py` (self-contained)
7. `attachments.py` (carved out of conversations)
8. `conversations.py` (large but cohesive — must be < 500 LoC after attachments split; if not, further split create/list/read from update/delete/retry)
9. `chat.py` (largest, most coupled — last)
10. Delete api.py shell.

**One commit per file**, named `refactor(api): split <module> into api/<file>`.

**Verification after Task 2.2:** `wc -l src/hermes/routes/api/*.py` — no file >500 LoC. If `chat.py` or `conversations.py` still exceeds, split further by sub-feature.

---

### Task 2.3: Split `personas.py` (695 LoC) — migrations + seeds out, resolver stays

**Files:**
- Create: `src/hermes/personas_migrations.py` — `_migrate_prompt_to_fragments`, `_drop_persona_skills_table`, `_migrate_skills_add_enabled`, `_migrate_personas_add_credential_columns`, `ensure_backfill` (lines 210-377)
- Create: `src/hermes/personas_seeds.py` — `CHANNEL_REGISTRY`, seed personas, bootstrap markdown constants (lines 61-207)
- Keep: `src/hermes/personas.py` — `PersonaContext`, `_persona_sections`, `_catalog_index`, `_resolve_*`, `get_effective_system_prompt`, `resolve_persona_context`, `resolve_chat_context_meta`, `ensure_bootstrap_skill_seeded`

**Steps:**
1. Extract seeds first (data only, zero risk).
2. Run `pytest tests/test_personas_*.py -q` → PASS.
3. Commit: `refactor(personas): extract seed data to personas_seeds`
4. Extract migrations.
5. Update wherever `ensure_backfill` is called (probably `db.py` or `main.py`) to import from `personas_migrations`.
6. Run `pytest tests/test_personas_*.py tests/test_personas_migration.py -q` → PASS.
7. Commit: `refactor(personas): extract one-shot migrations to personas_migrations`

**Verification:** `wc -l src/hermes/personas*.py` — all <500 LoC.

---

### Task 2.4: Split `starter_skills.py` (513 LoC) — one file per skill

**Files:**
- Create: `src/hermes/starter_skills/__init__.py` — re-export `STARTER_SKILLS` and `ensure_starter_skills_seeded()`
- Create: `src/hermes/starter_skills/<slug>.py` — one per skill (brainstorming, code_review, daily_journal, learn_explain, recipe_helper, socratic_dialogue, summarize_source, web_research)
- Delete: `src/hermes/starter_skills.py`

**Steps:**
1. Create the package directory + `__init__.py`.
2. Move each skill dict into `starter_skills/<slug>.py` as a module-level `SKILL` constant.
3. In `__init__.py`: `from .brainstorming import SKILL as _S1`, ..., `STARTER_SKILLS = [_S1, _S2, ...]`.
4. Move `ensure_starter_skills_seeded` to `__init__.py`.
5. Run `pytest -k starter -q` → PASS.
6. Commit: `refactor(skills): split starter_skills.py into one module per skill`

---

### Task 2.5 (OPTIONAL): Split `agent.py` (596 LoC)

agent.py is borderline (596 LoC) and tightly coupled. **Skip unless verification shows it benefits.** If we skip, document the decision in the final PR description.

If split is decided:
- `agent.py` keeps `run_agent`, `ApprovalDecision`, `Tool`, `ChatRunCancelled`, callback types.
- `agent_streaming.py` gets `_request_round_stream`, `_request_round_nonstream`.
- `agent_tools.py` gets `_format_tools`, `_parse_tool_arguments`, `_execute_tool_call`, `_denied_tool_result`, `_redact_persisted_tool_calls`.

**Default decision: SKIP.** 596 LoC is tolerable and the tight coupling between `run_agent` and the round handlers means a split creates more cross-module noise than it saves.

---

### Task 2.6 (OPTIONAL): `preferences.py` (674 LoC)

Borderline. Split only if Task 2.1-2.4 leaves headroom. If split:
- `preferences.py` → personas + channels routes
- `preferences_history.py` → snapshot/restore

**Default decision: SKIP.** 674 LoC with three distinct sections is acceptable; the file reads coherently as a single route module.

---

### Task 2.7: `schema.py` (615 LoC) — DO NOT SPLIT

DDL reference. One source of truth. Document this decision in the PR description: "schema.py kept intact — splitting trades cohesion for modularity without benefit."

---

## Phase 3: Test-file splits

### Task 3.1: Split `test_api_chat.py` (1533 LoC)

**Files:**
- Keep: `tests/test_api_chat.py` — core chat tests, auth, streaming basics
- Create: `tests/test_api_chat_message_ops.py` — cancel, retry, edit, touch (lines 202-290)
- Create: `tests/test_api_chat_approvals.py` — approval flow tests (lines 332-450)

**Steps:**
1. Identify clean test-class or top-level test-fn boundaries — DON'T split mid-class.
2. Move tests, keeping imports.
3. `pytest tests/test_api_chat*.py -q` → same test count as before, all PASS.
4. Commit: `refactor(tests): split test_api_chat into 3 files by concern`

---

### Task 3.2: Split `test_api_workspace.py` (1126 LoC)

**Files:**
- Keep: `tests/test_api_workspace.py` — browse tests (`/roots`, `/tree`, `/file` GET)
- Create: `tests/test_api_workspace_files.py` — file CRUD (`/file` POST/PUT/DELETE, `/rename`)
- Create: `tests/test_api_workspace_status.py` — `/git/status` (if not already in test_api_workspace_git.py)

**Steps:** same pattern. Commit: `refactor(tests): split test_api_workspace by operation class`

---

### Task 3.3: Split `test_api_workspace_git.py` (871 LoC)

**Files:**
- Keep: `tests/test_api_workspace_git.py` → rename to `test_api_workspace_git_inspect.py` — diff, branches, log
- Create: `tests/test_api_workspace_git_mutate.py` — checkout, stage, unstage, commit, push, pull, fetch
- Create: `tests/test_api_workspace_git_security.py` — traversal, injection, edge cases

Commit: `refactor(tests): split test_api_workspace_git by inspect/mutate/security`

---

### Task 3.4: Split `test_api_preferences.py` (744 LoC)

**Files:**
- Keep: `tests/test_api_preferences.py` → rename to `tests/test_api_personas.py` — persona CRUD, history
- Create: `tests/test_api_channels.py` — channel assignment
- Create: `tests/test_api_credentials.py` — credential binding (only if this section is genuinely separable; if it's intertwined with personas, leave it in test_api_personas.py)

Commit: `refactor(tests): split test_api_preferences into personas/channels/credentials`

---

### Task 3.5: Other test files — DO NOT SPLIT

- `test_agent_streaming.py` (885) — single cohesive contract test for `run_agent`. Keep.
- `test_approvals.py` (635) — well-organized by layer. Keep.
- `test_sandbox.py` (575) — `SandboxManager` contract. Keep.
- `test_api_runs.py` (572) — well-organized. Keep.
- `test_conversations.py` (535) — pure repo tests, cohesive. Keep.
- `test_api_mcp_servers.py` (526) — single endpoint. Keep.

Document this decision in the PR description.

---

## Phase 4: Verification + PR

### Task 4.1: Final verification

**Steps:**
1. `find src/hermes tests -name "*.py" -exec wc -l {} + | sort -rn | head -20`
   - Expected: no production file >500 LoC except `schema.py` (615, documented exception)
   - Expected: no test file >500 LoC except the 6 documented exceptions
2. `pytest -q` → all PASS, same test count as baseline
3. `ruff check src/ tests/` → clean
4. `mypy src/` (if used) → clean
5. `git log --oneline main..` → many small, atomic commits, each describing one move
6. Skim the full diff one more time looking for accidental behavior changes (e.g. an `if x is None:` becoming `if not x:`)

### Task 4.2: Open PR

**Steps:**
1. Push: `git push -u origin refactor/split-large-files-and-dry`
2. `gh pr create --title "refactor: split large files (>500 LoC) + DRY cleanup" --body "$(cat <<'EOF'`
3. PR body should include:
   - Before/after LoC table
   - List of new shared helpers (require_sandbox_manager, validate_limit, raise_error, drain_exec, decode_bytes)
   - List of files NOT split with rationale (schema.py, agent.py if skipped, the 6 test files)
   - Explicit confirmation: "No behavior changes. Full test suite passes. All commits atomic."

---

## Out of Scope (deferred)

These DRY findings were identified but deferred — they're either too risky or too low-value to bundle into this refactor:

1. **`_row_to_*` repo converters (16 files)** — Abstracting via SQLAlchemy ORM mapper is a larger architectural change; defer to its own RFC.
2. **DB connection boilerplate (`async with engine.connect()`, 53 sites)** — Per-function code is short; helper would obscure SQLAlchemy idiom. Skip.
3. **`raise_error` sweep across all 17 route files** — Task 1.3 applies it only to preferences.py as proof. Schedule the sweep as a separate follow-up commit IF the proof reads cleanly. Otherwise abandon.
4. **`get_or_404` helper** — Tempting but the existing patterns vary (some raise 404, some 410-gone, some return None and re-route). Don't force-fit.

If any of these become painful later, they're separately tractable.
