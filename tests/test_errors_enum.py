"""Structural guards for the `ErrorCode` enum (Plan 30 Task 2).

The enum is the single source of truth for user-actionable error tokens
emitted by HTTP routes. The frontend translates each value via an
`errors.<VALUE>` i18n key, so we enforce two invariants here:

1. Every member's value equals its name (StrEnum + UPPER_SNAKE).
   Drift between name and value would silently break the FE lookup.
2. The enum is non-empty — defends against an accidental cleanup
   that empties the registry without removing the import sites.

Coverage of *which* codes exist is enforced where they're used: the
`tests/test_api_*.py` suite asserts each code at its call site, so an
unused enum member shows up as a dead row in code review.
"""

from hermes.errors import ErrorCode


def test_error_code_values_match_names() -> None:
    for member in ErrorCode:
        assert member.value == member.name, (
            f"ErrorCode.{member.name} has divergent value {member.value!r} — "
            "StrEnum convention requires value == name so FE lookup "
            "(`errors.<value>`) stays stable when the enum is renamed."
        )


def test_error_code_enum_is_not_empty() -> None:
    assert len(list(ErrorCode)) > 0
