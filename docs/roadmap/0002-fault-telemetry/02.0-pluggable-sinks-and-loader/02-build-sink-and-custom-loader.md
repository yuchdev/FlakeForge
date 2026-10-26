# 02 - build_sink factory and rule_modules-style custom loader 🔒

**Parent task:** [README.md](README.md)
**Status:** ⬜ Not started

🔒 Security-sensitive: this subtask imports and runs project-controlled code and
needs a `security-auditor` pass (contract C7), exactly like Milestone 0001's 01.0/04.

## Requirements

- Implement `build_sink(spec, *, project_root) -> FaultEventSink` mapping the
  `telemetry_sink` string per the ADR table:
  - `""` -> `NullSink`
  - `"stdout"` -> `JsonlSink(sys.stdout)` (does not own the stream)
  - `"file:<path>"` -> `JsonlSink` over `<path>` opened in append mode (owns it)
  - `"sqlite:<path>"` -> `SqliteSink(<path>)`
  - `"logging"` -> `LoggingSink()`
  - any other string -> a **dotted import target** for a custom sink.
- For the custom case, **reuse the `rule_modules` loader**: import the dotted module
  with `registry._project_root_on_syspath(project_root)` open (the C7 window), and
  resolve either a zero-arg `build_sink()` factory **or** a zero-arg-constructable
  class exposing `emit`/`close` - mirroring `registry._load_register_function`'s
  "import a dotted name, use a well-known attribute" shape. Do not invent a second
  `sys.path`-mutating path (T7).
- `project_root` is the caller's `LintConfig.rule_module_root` (only a loaded config
  file vouches for its directory; `--no-config` passes `None`, so no project-local
  sink loads - same rule as rule modules).
- A spec that cannot be parsed, imported, or resolved to a conforming sink raises a
  clear error the CLI maps to exit `2` (C1), **before any file is checked** (T5).
  The error names the spec and the base_dir it was searched from, and must not echo
  the target module's source (same discipline as `_rule_module_load_message`).
- `file:`/`sqlite:` paths are output artifacts and are **not** root-confined like
  `include`/`baseline` (T8); absolute paths are allowed.

## Files

- `src/flakeforge/telemetry.py`.
- `tests/test_telemetry.py` + `tests/test_custom_sinks.py` (04.0): each built-in
  spec; a project-local custom sink loaded via a tmp module on `project_root`; a bad
  spec exits `2`; `--no-config` refuses a project-local sink.
- Update [`docs/custom-rules.md`](../../../custom-rules.md) Trust boundary section to
  note custom sinks load the same way and under the same trust boundary as rule
  modules.
