# Task 04.0 - Tests & Docs

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started

## Scope

Close the milestone: the conformance tripwire test, the per-sink/adapter/CLI test
coverage, and the user-facing docs. The conformance test is the only guard against
silent drift from DocSentinel's mirrored contract (there is no shared code to
enforce it).

## Subtasks

| # | Document | Status | Blocks |
|---|----------|--------|--------|
| 01 | [Conformance tripwire and test coverage](01-conformance-and-tests.md) | ⬜ | - |
| 02 | [Documentation](02-documentation.md) | ⬜ | - |

## Key constraints

- Coverage must not drop below the milestone-start value; `ruff` clean; telemetry
  code obeys the project's own rules.
- Docs must describe the Protocol, every reference sink, the custom-sink loader, the
  config/CLI surface, and the `flakeforge[telemetry]` extra, and must link the
  cross-repo spec doc so `/link-check` validates it.
