# 0003 - Telemetry Summary Document

> **Status:** Accepted
>
> **Date:** 2026-11-20
>
> **Supersedes:** _(none)_
>
> **Superseded by:** _(none)_

## Context

[ADR 0002](0002-fault-telemetry-event-emission.md) gave FlakeForge a per-evaluation
fault-event stream (`FaultEvent`, sinks, `events_for_result`) that shares a wire
contract with DocSentinel. A stream is the right shape for a collector, but a human or
a CI step wants the *statistics*: which generator model a run was attributed to, how
many findings each `XNNN` rule produced, which rules stayed clean, and where the
findings are. DocSentinel already renders such a view as its document-centric JSON
report (schema 2: `schema_version` / `run.model` / `summary.by_rule` / `rules` /
`documents[].problems[]`). FlakeForge should offer the same picture, shaped alike, so
the two tools' outputs can be read and compared side by side, and it should also be
derivable from an archive of many runs so "which model produces which faults" can be
charted over time.

Constraints carried over from ADR 0002: stdlib only, off by default, observational
(never a change to a result or exit code), no source text in any persisted artifact,
and no code shared with DocSentinel.

## Decision

1. **Derive the summary from the event stream, not from `LintResult`.**
   `telemetry.summarize_events(events)` aggregates `FaultEvent`s into a JSON-shaped
   mapping. The events already carry rule, fault class, tier, outcome, model,
   `details.path`/`line` and (on `pass` events) `files_checked`, so one adapter owns the
   semantics and the summary can never disagree with what a sink received. Runs are
   told apart by the per-run timestamp `events_for_result` stamps on every event.
2. **Mirror DocSentinel's report sections.** Top-level keys, in order:
   `schema_version` (integer, `1`, independent of the event contract's `"1.0"`),
   `event_schema_versions`, `domains`, `run` (`model`, per-model `models` table,
   `runs`, `first_ts`, `last_ts`), `summary` (`events`, `files_checked`,
   `files_with_problems`, `findings`, `by_outcome`, `by_rule`, `by_class`), `rules`
   (`evaluated`, `clean`, `fired`, `detail`) and `files[]` with `problems[]`.
   A *finding* is any non-`pass` event. Every table is key-sorted and files are
   path-sorted, so the document is deterministic.
3. **One config key, one flag.** `telemetry_summary` (`--telemetry-summary PATH`) names
   the per-run output document. It is orthogonal to `telemetry_sink`: either, both or
   neither may be set. `lint_paths` probes the path before linting (an unwritable path
   raises like a bad sink spec, exit `2`) without truncating an existing document, then
   materialises the events once, feeds the sink, and writes the summary; a failure
   while writing is logged and swallowed.
4. **Offline aggregation.** `telemetry.read_events(spec)` reads `file:`/bare-path JSON
   lines and `sqlite:` archives back into events (`event_from_wire` is the inverse of
   `to_wire_dict`); `flakeforge telemetry summary SOURCE...` concatenates sources and
   renders the same document as `text` or `json`. Malformed input is a
   `TelemetrySourceError` (exit `2`).

## Alternatives Considered

| Alternative | Pros | Cons | Reason rejected |
|-------------|------|------|-----------------|
| Build the summary from `LintResult` + registry (DocSentinel's `ReportModel` shape) | No event materialisation | A second classifier of outcomes/classes next to `events_for_result`; cannot summarise archives | Drift risk and no offline path |
| A `summary:<path>` sink spec | Reuses the sink lifecycle | Only one sink per run, so events *and* a summary need a composite spec grammar | Orthogonal key is simpler |
| Extend `--output-format json` with the statistics | No new surface | Mixes the lint report (schema 1, violations-centric) with telemetry; `--statistics` already covers per-code counts there | Keep report and telemetry separate |
| Add `files_checked` to every event's `details` so `files_checked` is always known | Simpler summariser | Changes the cross-tool event contract (minor bump coordinated with DocSentinel) | Out of scope; `null` when unknown instead |

## Consequences

### Positive

- One aggregation serves the per-run document and the multi-run archive view, and it
  reads like DocSentinel's JSON report.
- No new dependency, no change to the event wire contract, default-off, observational.
- The per-model table makes "which model produces which fault classes" a direct read.

### Negative

- Events are held in memory for a run when a summary is requested (bounded by rule
  count times file count; negligible for a linter).
- `files_checked` is `null` when a run recorded no `pass` event (every enabled rule
  fired), because only `pass` events carry it.
- Two runs within the same second are counted as one run.

## Validation / Rollout

- `tests/test_telemetry_summary.py` pins the document layout (tripwire on key order),
  the counting semantics, JSONL/SQLite round trips, error paths, the `lint_paths`
  wiring and both CLI surfaces; `tests/test_cli.py`'s template lockstep covers the
  config key; the wheel-smoke CI job exercises the flag and the subcommand end to end.
- Rollout is inert until `telemetry_summary` is set.

## Links

- **Prior ADR:** [0002 - Unified Fault-Telemetry Event Emission](0002-fault-telemetry-event-emission.md)
- **Supporting specs:** [`src/flakeforge/fault_classes.md`](../../src/flakeforge/fault_classes.md),
  DocSentinel's `docs/specs/fault-event-contract.md` (sibling repository, event wire contract)
- **Docs:** README "Telemetry summary", [`docs/architecture.md`](../architecture.md) "Fault telemetry"
