# FlakeForge rule-code -> fault-class mapping

This table maps every built-in `X###` rule (registered in
[`catalog.py`](catalog.py), implemented in [`rules.py`](rules.py)) to a stable
**fault class** used in the unified fault-telemetry event's `"class"` field. It is
the source of truth the `events_for_result` adapter in
[`telemetry.py`](telemetry.py) reads when it builds a `FaultEvent`.

See [`../../docs/adr/0002-fault-telemetry-event-emission.md`](../../docs/adr/0002-fault-telemetry-event-emission.md)
for the design and [`../../../DocSentinel/docs/specs/fault-event-contract.md`](../../../DocSentinel/docs/specs/fault-event-contract.md)
for the canonical cross-tool contract (that path points at the sibling DocSentinel
repository; there is no code shared between the two tools, only this common event
vocabulary).

All FlakeForge rules are deterministic AST/token checks, so every event derived
from this table carries `domain="code"` and `tier="deterministic"`.

## Mapping

| Rule | Description | Fault class |
|------|-------------|-------------|
| `X001` | Do not use bare except | `broad_exception_handling` |
| `X002` | Do not use `except Exception` | `broad_exception_handling` |
| `X003` | Do not create circular imports | `import_structure` |
| `X004` | Do not silently swallow exceptions | `swallowed_error` |
| `X005` | Require compliant docstrings | `missing_documentation` |
| `X006` | Do not use local imports | `import_structure` |
| `X007` | Require return annotations for value-returning functions | `missing_type_annotation` |
| `X008` | Do not annotate returns with `None` | `misleading_type_annotation` |
| `X009` | Do not use percent formatting | `legacy_formatting` |
| `X010` | Do not suppress `ImportError` | `swallowed_error` |
| `X011` | Do not use `Type | None` | `annotation_style_violation` |
| `X012` | Do not use `Type1 | Type2` | `annotation_style_violation` |
| `X013` | Require RAII context management for `subprocess.Popen`/`socket.socket` | `resource_leak_risk` |
| `X014` | Enforce tracked TODO/FIXME metadata in comments | `untracked_work_marker` |
| `X015` | Report unused `# noqa` directives | `stale_suppression` |

## Fault-class vocabulary

The classes above collapse the 15 rule codes into a smaller, aggregation-friendly
vocabulary a collector can chart over time:

| Fault class | Rules | What it counts |
|-------------|-------|----------------|
| `broad_exception_handling` | X001, X002 | handlers too broad to be meaningful |
| `swallowed_error` | X004, X010 | errors silenced instead of handled or surfaced |
| `import_structure` | X003, X006 | import-graph and import-placement faults |
| `missing_documentation` | X005 | absent/non-compliant docstrings |
| `missing_type_annotation` | X007 | absent type annotations |
| `misleading_type_annotation` | X008 | present but wrong/redundant annotations |
| `annotation_style_violation` | X011, X012 | annotation style the project forbids |
| `legacy_formatting` | X009 | outdated string-formatting idioms |
| `resource_leak_risk` | X013 | resources not managed via RAII context managers |
| `untracked_work_marker` | X014 | TODO/FIXME lacking tracked metadata |
| `stale_suppression` | X015 | suppression directives that no longer suppress anything |
| `unmapped` | _(fallback)_ | any registered code with no entry here |

## Maintenance contract

- **Append-only, like rule codes.** Adding a rule means adding a row here in the
  same change. A new class name is fine; renaming an existing class is a breaking
  change to the cross-tool contract and requires a `schema_version` bump
  coordinated with DocSentinel and the spec doc (see the ADR's Consequences).
- **`unmapped` is the fallback** for any registered code without a row. The
  conformance test asserts every registered built-in code resolves to something
  other than `unmapped`, so a rule shipped without a mapping entry fails CI rather
  than mis-aggregating silently.
- Project-local or entry-point `rule_modules` rules are **not** in this table; the
  adapter maps any code it does not recognise to `unmapped`. A future revision may
  let a provider contribute its own mapping.
