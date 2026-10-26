# Milestone 0002 - Fault Telemetry

Index for the fault-telemetry milestone. See [plan.md](plan.md) for the full plan,
shared contracts, and task table, and [status.md](status.md) for progress.

This milestone gives `flakeforge` a unified, structured fault-telemetry event
emitted whenever a rule evaluates code, so fault-mode rates become measurable over
time. The event vocabulary is shared with the sibling DocSentinel project through a
written specification - **no code is shared between the two repos**. The design is
recorded in [ADR 0002](../../adr/0002-fault-telemetry-event-emission.md); the
canonical cross-tool contract lives in DocSentinel's
[`docs/specs/fault-event-contract.md`](../../../../DocSentinel/docs/specs/fault-event-contract.md).

## Tasks

| Task | Name | Output |
|------|------|--------|
| [01.0](01.0-faultevent-and-sink-protocol/README.md) | FaultEvent & Sink Protocol | `telemetry.py` with `FaultEvent`, `to_wire_dict`, the `FaultEventSink` Protocol, and the `events_for_result` adapter |
| [02.0](02.0-pluggable-sinks-and-loader/README.md) | Pluggable Sinks & Loader | `NullSink`/`LoggingSink`/`JsonlSink`/`SqliteSink`, `build_sink`, and the `rule_modules`-style custom-sink loader |
| [03.0](03.0-cli-and-config-surface/README.md) | CLI & Config Surface | `telemetry_sink`/`generated_by_model` config keys, `--telemetry-sink`/`--generated-by-model` flags, the `flakeforge[telemetry]` extra, and the `lint_paths` call site |
| [04.0](04.0-tests-and-docs/README.md) | Tests & Docs | the conformance tripwire test, per-sink and adapter tests, and README/architecture/custom-rules docs |
