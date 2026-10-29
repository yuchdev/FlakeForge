# 01 - FaultEvent dataclass and to_wire_dict

**Parent task:** [README.md](README.md)
**Status:** ✅ Complete

## Requirements

- Define `FaultEvent` as a frozen dataclass in `src/flakeforge/telemetry.py` with
  the fields in the ADR's section 1 (`ts`, `domain`, `fault_class`, `tier`, `rule`,
  `outcome`, `model: Optional[str]`, `schema_version: str = "1.0"`,
  `details: Mapping[...] = field(default_factory=dict)`).
- `details` values are restricted to JSON primitives
  (`Union[str, int, float, bool, None]`) by type annotation and by contract (T8):
  never source text, file contents, or a matched snippet.
- `SCHEMA_VERSION = "1.0"` as a module constant; `FaultEvent.schema_version`
  defaults to it. It is a **string** and must not reference the integer
  `api.JSON_SCHEMA_VERSION`.
- Provide `to_wire_dict(event: FaultEvent) -> dict[str, object]` that returns the
  wire mapping with `fault_class` renamed to the key `"class"` and key order
  matching the spec. It performs no I/O.
- A module constant for the `ts` factory (`datetime.now(timezone.utc)` rendered
  RFC-3339 with a `Z` suffix) so construction is testable with a fixed clock.

## Non-goals

- No sinks, no serialization to a file, no CLI/config.

## Files

- `src/flakeforge/telemetry.py` (new).
- `tests/test_telemetry.py` (new, in 04.0): event construction, `to_wire_dict` key
  rename, the `details` primitive bound, and `schema_version` literal.
