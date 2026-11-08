# Task 01.0 - FaultEvent & Sink Protocol

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started

## Scope

Create `src/flakeforge/telemetry.py` and define the data contract and the adapter
that turns a finished `LintResult` into events. This is the foundation every other
task builds on. No sinks, no CLI, no config yet - just the event, its wire form,
the Protocol, and the mapping logic.

Honours shared contracts T1 (no DocSentinel import), T2 (wire schema), T3 (outcome
vocabulary), T4 (stdlib-only), and T8 (no secrets in `details`).

## Subtasks

| # | Document | Status | Blocks |
|---|----------|--------|--------|
| 01 | [FaultEvent dataclass and to_wire_dict](01-faultevent-and-wire-dict.md) | ⬜ | 02, 02.0, 03.0 |
| 02 | [FaultEventSink Protocol and events_for_result adapter](02-sink-protocol-and-adapter.md) | ⬜ | 02.0, 03.0 |

## Key constraints

- `telemetry.py` imports only the stdlib plus FlakeForge's own
  `LintResult`/`LintConfig`/`RuleRegistry`/`RuleViolation` types - never anything
  from DocSentinel (T1).
- `FaultEvent` is a frozen dataclass (house style; matches `RuleViolation`,
  `LintResult`). Use `Optional[T]`, never `T | None` (the project's own X011).
- `tier` and `domain` are constants baked into the adapter, not parameters.
