# Milestone 0002 - Fault Telemetry

**Package:** `flakeforge` | **Module root:** `src/flakeforge/`

Give `flakeforge` a single, structured **fault-telemetry event** emitted whenever a
rule evaluates code, so a downstream collector can measure fault-mode rates over
time (broad-except rate, swallowed-error rate, resource-leak-risk rate, stale
suppressions, and so on) instead of reading a one-shot pass/fail. The event shape
and vocabulary are **shared with the sibling DocSentinel project** so a collector
can aggregate a mixed stream from both tools - but the two tools **share no code**.
The contract is a written specification; FlakeForge implements it independently.

Design of record: [ADR 0002](../../adr/0002-fault-telemetry-event-emission.md).
Canonical cross-tool contract (other repo, no import):
[`../../../../DocSentinel/docs/specs/fault-event-contract.md`](../../../../DocSentinel/docs/specs/fault-event-contract.md).
Fault-class mapping: [`src/flakeforge/fault_classes.md`](../../../src/flakeforge/fault_classes.md).

## Why now

Each rule run answers one pass/fail question and discards the detail. Once both
gates emit a uniform event, a model upgrade becomes measurable: "the fabricated-
reference rate dropped" (DocSentinel) and "the swallowed-error rate dropped"
(FlakeForge) become numbers on the same stream. FlakeForge is the
**always-deterministic baseline** in that stream (`tier="deterministic"` for every
event), against which DocSentinel's mixed deterministic/heuristic tiers compare.

## Tasks

| Task | Name | Category | Depends on | Output |
|------|------|----------|------------|--------|
| 01.0 | FaultEvent & Sink Protocol | feature | - | `telemetry.py`: `FaultEvent` frozen dataclass, `to_wire_dict`, `FaultEventSink` Protocol, `events_for_result` adapter |
| 02.0 | Pluggable Sinks & Loader | feature | 01.0 | `NullSink`/`LoggingSink`/`JsonlSink`/`SqliteSink`, `build_sink` factory, `rule_modules`-style custom-sink loader |
| 03.0 | CLI & Config Surface | feature | 01.0, 02.0 | `telemetry_sink`/`generated_by_model` schema keys + flags, `flakeforge[telemetry]` extra, single `lint_paths` emission call site |
| 04.0 | Tests & Docs | feature | 01.0, 02.0, 03.0 | conformance tripwire, per-sink/adapter/CLI tests, README + architecture + custom-rules docs |

01.0 is the foundation; 02.0 and 03.0 build on it; 04.0 closes the milestone.

### Dependency graph

```
01.0 ──► 02.0 ──► 03.0 ──► 04.0
  └───────────────► 03.0
```

## Shared contracts (authoritative)

These bind every task. They extend, and never weaken, Milestone 0001's contracts
(C1 exit codes, C7 rule-module trust boundary, C8 zero runtime dependencies).

- **T1 - No shared code with DocSentinel.** The cross-tool contract is a written
  spec. FlakeForge defines its own `FaultEvent` and `FaultEventSink`. **No runtime
  import crosses the two repos.** Conformance is by matching field names, types and
  vocabularies only.
- **T2 - Wire schema is fixed by the spec.** Keys: `ts`, `domain`, `class`, `tier`,
  `rule`, `outcome`, `model`, `schema_version`, `details`. The Python attribute is
  `fault_class`; `to_wire_dict` renames it to `"class"`. `domain="code"` and
  `tier="deterministic"` are constants for FlakeForge. `schema_version="1.0"` is a
  **string** and is independent of the integer `api.JSON_SCHEMA_VERSION`.
- **T3 - Outcome vocabulary.** `pass` | `rejected` | `suppressed` | `advisory` |
  `escalated`. `escalated` is **reserved and unused** on FlakeForge (no HITL flow).
  See the ADR's outcome table for each value's FlakeForge meaning.
- **T4 - Zero runtime dependencies (C8 holds).** The module and all four reference
  sinks are stdlib-only (`dataclasses`, `json`, `datetime`, `logging`, `sqlite3`,
  `pathlib`, `importlib`). `flakeforge[telemetry]` is an **empty** extra;
  `[project].dependencies` stays `[]`. Postgres/other DB sinks are bring-your-own,
  no client dependency added.
- **T5 - Opt-in and non-blocking.** Default off (`telemetry_sink=""` -> `NullSink`).
  Emission never changes the return value or the exit code and **never raises into
  the caller**; a sink error during emission is swallowed. A bad sink *spec* is a
  config error (exit `2`, C1) raised before any file is checked.
- **T6 - One call site.** Events are emitted from exactly one place,
  `api.lint_paths()`, reusing the assembled `LintResult`. Rules never emit. The
  Flake8 plugin and pytest helper bypass `lint_paths` and emit nothing in v1.
- **T7 - Custom-sink loader reuses `rule_modules`.** A `telemetry_sink` that is not
  a built-in keyword is a dotted import target loaded through the same mechanism and
  the same C7 trust boundary (`_project_root_on_syspath`, `rule_module_root`) as
  rule modules. No second code-loading path is invented.
- **T8 - No secrets/PII.** `details` is bounded to JSON primitives and must never
  carry source text, file contents, or matched snippets - only codes, counts, and
  already-`base_dir`-relative display paths the normal report would print. The
  `telemetry_sink` `file:`/`sqlite:` path is an output artifact (not root-confined
  like `include`/`baseline`).

## Open decisions (ratify before the dependent subtask starts)

- **D1 - Does `advisory` map to the `--write-baseline` capture run?** The ADR
  proposes yes: a capture run records violations but exits `0`, so they are accepted,
  not rejected. Alternative: leave `advisory` unused on FlakeForge too and only
  DocSentinel populates it. Recommendation: (a) adopt the `--write-baseline` mapping,
  so the value has a real FlakeForge home. Ratify before 03.0.
- **D2 - `noqa`/`per_file_ignores` as `suppressed`.** v1 maps only baseline drops to
  `suppressed` (the only suppression surfaced on `LintResult`). Surfacing
  `check_tree`'s per-line covered set to also emit `suppressed` for `noqa`/per-file
  drops is a follow-up. Recommendation: defer to a later milestone; do not expand the
  `check_tree` return contract in 0002.

## Milestone exit gates

1. `pytest --cov=flakeforge` green on Python 3.11-3.14; line coverage not below the
   value at milestone start.
2. `ruff check .` and the `flakeforge` self-check subset used in CI are clean.
   Telemetry code obeys the project's own rules (`Optional[T]`, no `print`
   diagnostics - sinks use `logging`/streams, RAII for the SQLite/file handles).
3. With `telemetry_sink` unset, every existing test and output is byte-identical
   (telemetry is inert).
4. The conformance tripwire test passes and is wired into CI.
5. README, `docs/architecture.md`, and `docs/custom-rules.md` describe telemetry and
   the custom-sink Protocol. `/link-check` is clean, including the cross-repo
   reference to DocSentinel's spec doc.
