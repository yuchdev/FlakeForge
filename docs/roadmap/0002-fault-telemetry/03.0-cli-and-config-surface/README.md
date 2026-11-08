# Task 03.0 - CLI & Config Surface

**Parent milestone:** [plan.md](../plan.md)
**Status:** ⬜ Not started

## Scope

Expose telemetry through config and the CLI, add the `flakeforge[telemetry]` extra,
and wire the single emission call site into `api.lint_paths()`. After this task,
`flakeforge check --telemetry-sink <spec> --generated-by-model <id>` emits events;
without a sink spec nothing changes (T5).

## Subtasks

| # | Document | Status | Blocks |
|---|----------|--------|--------|
| 01 | [Config keys, CLI flags, and the telemetry extra](01-config-cli-and-extra.md) | ⬜ | 02 |
| 02 | [Single emission call site in lint_paths](02-lint-paths-emission.md) | ⬜ | 04.0 |

## Key constraints

- `telemetry_sink`/`generated_by_model` join `_CONFIG_SCHEMA` and the `init`
  template (the schema/template lockstep test must stay green, C5).
- Flags live on `check` only, not the shared parent parser (`rules`/`config show`
  do not emit). Overlay via `LintConfig.merge`; record in `_cli_override_keys`.
- Emission is swallow-on-error and skipped when disabled (T5); it reuses the
  assembled `LintResult` and never re-iterates (T6).
- `[project].dependencies` stays `[]`; the extra is empty (C8/T4).
