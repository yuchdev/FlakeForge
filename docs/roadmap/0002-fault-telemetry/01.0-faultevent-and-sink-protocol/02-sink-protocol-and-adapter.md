# 02 - FaultEventSink Protocol and events_for_result adapter

**Parent task:** [README.md](README.md)
**Status:** ✅ Complete

## Requirements

- Define `FaultEventSink(Protocol)` with `emit(self, event: FaultEvent) -> None`
  and `close(self) -> None`. PEP 544 structural typing - no base class, no
  inheritance required of implementers (T1-adjacent: a user sink imports nothing).
- Implement `events_for_result(result, registry, config, *, model) -> Iterator[FaultEvent]`
  reading the **already-assembled** `LintResult`; it must not re-run rules or
  re-iterate files.
- Map fields per the ADR:
  - `domain="code"`, `tier="deterministic"` (constants).
  - `fault_class` from the `fault_classes.md` table; unknown code -> `"unmapped"`.
    Load the mapping as an in-module dict keyed by rule code (kept in step with the
    doc; the conformance test asserts parity).
  - `model` = `model` arg or `None` when empty.
- Emit outcomes (T3) from the finished result:
  - one `rejected` per reported `RuleViolation`;
  - one `suppressed` per baseline-suppressed finding (entries in
    `result.fingerprints` absent from the reported set);
  - one `pass` per enabled, evaluated rule with no surviving violation (use the
    registry + `_is_rule_enabled`, the same enablement `check` uses);
  - `advisory` is produced only on a `--write-baseline` capture run - the adapter
    takes a flag/signal from the call site for that mode (see 03.0), pending D1.
  - `escalated` is never produced (reserved, T3).
- `details` carries only the rule code and `base_dir`-relative display path and
  counts - no source text (T8).

## Non-goals

- No sink implementations (02.0) and no `lint_paths` wiring (03.0). The adapter is a
  pure function from a result to an event iterator.

## Files

- `src/flakeforge/telemetry.py`.
- `tests/test_telemetry.py` (04.0): outcome mapping across clean/violation/baseline
  cases, the `unmapped` fallback, and the `details` no-source-text guard.
