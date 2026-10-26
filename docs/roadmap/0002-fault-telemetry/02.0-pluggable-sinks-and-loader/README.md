# Task 02.0 - Pluggable Sinks & Loader

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started

## Scope

Implement the four reference sinks, the `build_sink` factory that maps a
`telemetry_sink` spec string to a sink, and the custom-sink loader that reuses the
`rule_modules` import mechanism and its C7 trust boundary. All stdlib-only (T4). No
database-client dependency is added - SQLite is the stdlib example; Postgres/others
are bring-your-own via the Protocol.

## Subtasks

| # | Document | Status | Blocks |
|---|----------|--------|--------|
| 01 | [Reference sinks (Null/Logging/Jsonl/Sqlite)](01-reference-sinks.md) | ⬜ | 02 |
| 02 | [build_sink factory and rule_modules-style custom loader](02-build-sink-and-custom-loader.md) 🔒 | ⬜ | 03.0 |

🔒 = security-sensitive: the custom-sink loader imports and executes project code and
needs the same trust-boundary care as Milestone 0001's 01.0/04 (contract C7).

## Key constraints

- Sinks satisfy `FaultEventSink` structurally; `JsonlSink`/`SqliteSink` manage their
  own handles via `close()` (RAII; the project forbids leaked resources - its own
  X013).
- `LoggingSink` uses stdlib `logging`, never `print` (the project's no-`print`
  rule).
- The loader reuses `registry._project_root_on_syspath` / the
  `LintConfig.rule_module_root` policy; it must not introduce a second
  `sys.path`-mutating path (T7).
