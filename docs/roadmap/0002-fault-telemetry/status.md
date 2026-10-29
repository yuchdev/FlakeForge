# Milestone 0002 - Fault Telemetry - Status

Tracks progress against [plan.md](plan.md). Updated as each task lands.

## Current status

| Task | Name | Status | Tests |
|------|------|--------|-------|
| 01.0 | FaultEvent & Sink Protocol | ✅ Complete | 453 passed |
| 02.0 | Pluggable Sinks & Loader | ✅ Complete | 453 passed |
| 03.0 | CLI & Config Surface | ✅ Complete | 453 passed |
| 04.0 | Tests & Docs | ✅ Complete | 453 passed |

**Legend:** ✅ Complete · 🔶 In progress / partial · ⬜ Not started · ⏸ Deferred (ratified)

## Notes & decisions

- 2026-10-26 - Milestone proposed in [ADR 0002](../../adr/0002-fault-telemetry-event-emission.md)
  (Status: Proposed). Architecture/planning only; no code written yet.
- 2026-10-26 - **D1 ratified (a):** `advisory` = `--write-baseline` capture run. **D2 deferred**
  (no `check_tree` change).
- 2026-10-26 - As-built deviation: `BaselineEntry` fingerprints carry no rule code, so
  `lint_paths` passes the baseline-dropped violations (`Counter(all) - Counter(reported)`)
  to `events_for_result(suppressed=..., capture_run=...)`. Sink spec is built before any
  file is read; `"mod.attr"`, `"mod:attr"` and `"mod"` (with `build_sink`) are accepted.
- 2026-10-26 - Cross-tool contract is a **written spec** in the DocSentinel repo; no
  code is shared. The conformance test (04.0) guards FlakeForge's own literals only.

**Gates (2026-10-26, Python 3.12 only):** pytest 453 passed (telemetry.py 97% cov), `ruff check .`
clean, `flakeforge check` (pyproject settings) clean, `python -m build` OK. Not run: 3.11/3.13/3.14,
the wheel-smoke job itself (assertions added to ci.yml), `/pr-review`, `/link-check`.
