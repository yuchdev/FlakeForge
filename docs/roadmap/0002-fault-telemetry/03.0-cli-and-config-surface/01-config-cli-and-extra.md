# 01 - Config keys, CLI flags, and the telemetry extra

**Parent task:** [README.md](README.md)
**Status:** ✅ Complete

## Requirements

- Add two keys to `config._CONFIG_SCHEMA` and matching `LintConfig` fields +
  `merge` parameters:
  - `telemetry_sink: str = ""` via `_as_str` - the sink spec (ADR section 3). It is
    an output-artifact spec, **not** a root-confined input path, so it uses
    `_as_str`, never `_as_config_path`; absolute `file:`/`sqlite:` paths are allowed
    (T8).
  - `generated_by_model: str = ""` via `_as_str` - free string; `""` means
    `model: null`.
- Add both keys to the `init` starter template (`_CONFIG_TEMPLATE_BODY`) with one
  comment line each, so the schema/template lockstep test (`CONFIG_KEYS` ==
  template keys) stays green (C5).
- Add flags to the `check` subparser only (not `_add_common_arguments`):
  - `--telemetry-sink SPEC` -> `telemetry_sink`
  - `--generated-by-model ID` -> `generated_by_model`
  Overlay through `_build_cli_config` -> `LintConfig.merge`, with `None`-means-unset
  precedence (C2), and add them to `_cli_override_keys` so `config show` attributes
  their origin.
- Add `telemetry = []` to `[project.optional-dependencies]` in `pyproject.toml`.
  `[project].dependencies` stays `[]` (C8).

## Non-goals

- No emission yet (subtask 02). This task only threads the two values to where
  `lint_paths` can read them.

## Files

- `src/flakeforge/config.py`, `src/flakeforge/cli.py`, `pyproject.toml`.
- `tests/test_config.py`, `tests/test_cli.py` (04.0): both keys parse/validate;
  template round-trips; `config show` reports them; absolute `file:`/`sqlite:` paths
  are accepted.
