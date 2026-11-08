# 02 - Documentation

**Parent task:** [README.md](README.md)
**Status:** ✅ Complete

## Requirements

- **README**: a "Fault telemetry" section - what the event is, the `flakeforge[telemetry]`
  extra, the `telemetry_sink` spec table (`stdout`/`file:`/`sqlite:`/`logging`/dotted
  custom), `--telemetry-sink` / `--generated-by-model`, and a one-line note that the
  event vocabulary is shared with DocSentinel through a written spec (no shared code).
  State that FlakeForge events are always `tier="deterministic"`.
- **`docs/architecture.md`**: the single `lint_paths` emission call site, the
  swallow-on-error / default-off behaviour, and why rules never emit.
- **`docs/custom-rules.md`**: a "Custom telemetry sinks" subsection showing the
  `FaultEventSink` Protocol, a minimal custom-sink example, and that custom sinks
  load through the same `rule_modules` mechanism and trust boundary (C7) - with a
  cross-link to the existing Trust boundary section.
- Link [ADR 0002](../../../adr/0002-fault-telemetry-event-emission.md),
  [`src/flakeforge/fault_classes.md`](../../../../src/flakeforge/fault_classes.md),
  and the canonical spec
  [`../../../../DocSentinel/docs/specs/fault-event-contract.md`](../../../../../DocSentinel/docs/specs/fault-event-contract.md)
  so `/link-check` validates them.

## Files

- `README.md`, `docs/architecture.md`, `docs/custom-rules.md`.
