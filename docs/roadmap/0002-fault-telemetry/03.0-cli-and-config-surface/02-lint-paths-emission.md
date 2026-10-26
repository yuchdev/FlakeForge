# 02 - Single emission call site in lint_paths

**Parent task:** [README.md](README.md)
**Status:** ⬜ Not started

## Requirements

- In `api.lint_paths()`, **after** the `LintResult` is assembled and **before** it
  is returned, build a sink with `telemetry.build_sink(config.telemetry_sink,
  project_root=config.rule_module_root)` and feed it
  `telemetry.events_for_result(result, registry, config, model=config.generated_by_model or None)`.
  Call `sink.close()` in a `finally`.
- This is the **only** emission call site (T6). Rules never emit. The Flake8 plugin
  (`plugin.py`) and pytest helper (`testing.py`) bypass `lint_paths` and emit
  nothing in v1 - note this in their docstrings so the silence is intentional.
- **Non-blocking (T5):** wrap emission so a sink error during `emit`/`close` is
  caught and swallowed (observational, not a gate); it must not change the return
  value or the exit code. When `telemetry_sink == ""`, skip the whole block
  (`build_sink` returns `NullSink`, but skip early to avoid constructing the event
  iterator at all).
- A bad `telemetry_sink` spec surfaces as a load error from `build_sink`. Decide
  where it is raised: it should be caught by the CLI's existing
  `except (OSError, ValueError, RuntimeError, SyntaxError)` and exit `2` (C1),
  **before** files are checked - so resolve/validate the sink spec at the top of
  `_run_check`, not mid-run. Keep `lint_paths`-as-library usable: a library caller
  that passes a bad spec gets the raised error, not a swallowed one.
- The `--write-baseline` capture run signals `advisory` mode to the adapter (D1,
  pending ratification): on that path, emit captured violations as `advisory`
  instead of `rejected` and do not apply a baseline.

## Files

- `src/flakeforge/api.py`, `src/flakeforge/cli.py`.
- `src/flakeforge/plugin.py`, `src/flakeforge/testing.py` (docstring notes only).
- `tests/test_api.py`, `tests/test_cli.py`, `tests/mock/` (04.0): disabled run
  emits nothing and is byte-identical; a run with a sink emits
  rejected/suppressed/pass; a `--write-baseline` run emits advisory; an emission
  error is swallowed (exit code unchanged); a bad spec exits `2` before any file is
  read.
