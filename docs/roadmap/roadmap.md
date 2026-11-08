## 📋 Roadmap Phase 1: Generic Implementation

[Milestone 0001 - Generic Implementation](0001-generic-implementation/plan.md) ·
[status](0001-generic-implementation/status.md) ·
**✅ Complete (2026-09-25)**: tasks 01.0-05.0 delivered; 06.0 deferred.

Makes `flakeforge` a fully standalone CLI (target directory + a TOML file of CLI
parameters) and makes project setup through `pyproject.toml` / `flakeforge.toml`
strict and predictable. Then it adds introspection, CI output formats, incremental
adoption, and scale and distribution improvements.

## 📋 Roadmap Phase 2: Observability

[Milestone 0002 - Fault Telemetry](0002-fault-telemetry/plan.md) ·
[status](0002-fault-telemetry/status.md) ·
**⬜ Proposed**: tasks 01.0-04.0 planned; see [ADR 0002](../adr/0002-fault-telemetry-event-emission.md).

Adds a unified, structured fault-telemetry event emitted whenever a rule evaluates
code, so fault-mode rates (broad-except, swallowed-error, resource-leak-risk, stale
suppression, ...) become measurable over time. The event vocabulary is shared with
the sibling DocSentinel project through a written specification - no code is shared
between the two repos. FlakeForge is the always-`deterministic` baseline in a mixed
stream. Introduces `telemetry.py` (`FaultEvent` + `FaultEventSink` Protocol + the
`NullSink`/`LoggingSink`/`JsonlSink`/`SqliteSink` reference sinks + a
`rule_modules`-style custom-sink loader), the `telemetry_sink`/`generated_by_model`
config/CLI surface, and the empty `flakeforge[telemetry]` opt-in extra.
