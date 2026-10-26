# 01 - Reference sinks (Null/Logging/Jsonl/Sqlite)

**Parent task:** [README.md](README.md)
**Status:** ⬜ Not started

## Requirements

Implement in `src/flakeforge/telemetry.py`, each satisfying `FaultEventSink`:

- **`NullSink`** - `emit`/`close` are no-ops. The default when telemetry is off, so
  the engine never special-cases a disabled run.
- **`LoggingSink(logger=None, level=logging.INFO)`** - logs
  `json.dumps(to_wire_dict(event))` through a stdlib `logging.Logger`
  (`"flakeforge.telemetry"` by default) at `level`. `close` is a no-op. No `print`.
- **`JsonlSink(stream, *, owns_stream=False)`** - writes one compact JSON line per
  `emit` and flushes; `close` closes the stream only when `owns_stream` is true
  (so `sys.stdout` is never closed; an opened file is).
- **`SqliteSink(path)`** - stdlib `sqlite3`. Creates `fault_events` (one column per
  top-level field; `details` as a JSON-text column) if absent, inserts one row per
  `emit`, commits, and `close`s the connection. **No third-party DB client.**
  Document in a docstring that Postgres/other DBs are bring-your-own sinks following
  the same Protocol.

## Non-goals

- No spec-string parsing or loader (that is subtask 02). Each sink is constructed
  directly with its own arguments here.

## Files

- `src/flakeforge/telemetry.py`.
- `tests/test_telemetry.py` (04.0): round-trip each sink; assert `JsonlSink` leaves
  `sys.stdout` open and closes an owned file; assert `SqliteSink` writes one row per
  event with `details` as JSON text.
