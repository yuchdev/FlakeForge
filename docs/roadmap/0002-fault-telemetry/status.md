# Milestone 0002 - Fault Telemetry - Status

Tracks progress against [plan.md](plan.md). Updated as each task lands.

## Current status

| Task | Name | Status | Tests |
|------|------|--------|-------|
| 01.0 | FaultEvent & Sink Protocol | ⬜ Not started | - |
| 02.0 | Pluggable Sinks & Loader | ⬜ Not started | - |
| 03.0 | CLI & Config Surface | ⬜ Not started | - |
| 04.0 | Tests & Docs | ⬜ Not started | - |

**Legend:** ✅ Complete · 🔶 In progress / partial · ⬜ Not started · ⏸ Deferred (ratified)

## Notes & decisions

- 2026-10-26 - Milestone proposed in [ADR 0002](../../adr/0002-fault-telemetry-event-emission.md)
  (Status: Proposed). Architecture/planning only; no code written yet.
- 2026-10-26 - **D1 / D2 are open** (see plan.md "Open decisions"); ratify D1 before
  03.0 and D2 before any `check_tree` contract change (recommended: defer D2).
- 2026-10-26 - Cross-tool contract is a **written spec** in the DocSentinel repo; no
  code is shared. The conformance test (04.0) guards FlakeForge's own literals only.
