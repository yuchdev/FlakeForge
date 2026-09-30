# Changelog

## Unreleased

- **Internal: split `flakeforge.rules`.** `rules.py` now contains only the built-in
  rule functions in ascending code order; shared helpers and constants moved to
  `flakeforge.rule_helpers`, and `CallbackRule` / `builtin_registrations()` moved
  to `flakeforge.catalog`. `ModuleImport`, `ModuleLocation`, `ModuleImportGraph`,
  `module_location`, `extract_module_imports` and `build_import_graph` remain
  importable from `flakeforge.rules`. No rule behavior changes.
- **New built-in `X015` for unused `# noqa` directives (enabled by default).** The
  engine now reports `X015` for any `# noqa` that suppressed nothing on a run — a
  bare `# noqa` where nothing on the line was suppressed, or a coded `# noqa: CODE`
  whose code is unknown or matched no suppressed violation. A coded directive that
  names a registered but currently *disabled* code (via `select` / `ignore`) is
  **not** flagged, since another run — for example a CI `--select` subset — may
  still use it; a *bare* `# noqa` names no codes and so can read as unused under a
  narrow `select`. A violation dropped by `per_file_ignores` still counts as
  covering the `# noqa` on its line. `X015` is emitted engine-side (not by an
  AST rule), is not itself suppressible by `# noqa`, is skipped whenever `# noqa`
  is inert for a file (`allow_noqa = false` or a `noqa_allowed` / `noqa_forbidden`
  path policy), and still honours `select` / `ignore`, `per_file_ignores`, and the
  baseline. Flake8 owns `# noqa`, so the Flake8 adapter never emits it. This is a
  user-visible behavior change; opt out with `ignore = ["X015"]`.
- **New baseline file for incremental adoption.** `--write-baseline PATH`
  records every current violation's fingerprint (after select/ignore,
  `per_file_ignores`, and `# noqa`) to a deterministic, byte-stable JSON file and
  exits `0`. `--baseline PATH` (TOML key `baseline`, resolved relative to the
  config file's directory, contract C6; absolute config values are rejected)
  suppresses any violation recorded in it, so only *new* violations count toward
  exit `1` (contract C1). The fingerprint is
  `sha256(code + relative path + normalized source line text)` plus an occurrence
  index (decision D2), so a suppression survives inserting lines above the
  violation but re-surfaces when the flagged line is edited. A baseline entry that
  no longer matches is reported as "fixed" — a count in text output and a list
  under the additive `baseline` JSON key (`github`/`sarif` omit it) — and never
  fails the run. A missing or malformed baseline exits `2`. `config show` reports
  the `baseline` key with its origin.
- **New `per_file_ignores` config key and `--per-file-ignores` flag.** A TOML
  table mapping a path glob to rule-code prefixes skipped for matching files,
  e.g. `per_file_ignores = { "tests/**" = ["X002"], "scripts/*.py" = ["X0"] }`.
  The globs resolve against the config file's directory like `include`/`exclude`
  (contract C6) — absolute globs are a config error (exit `2`) — and the codes
  are validated like `ignore` (an unknown selector exits `2`; the legacy
  `[tool.flake8_lint]` section stays lenient and drops unknowns with a warning).
  The engine applies the suppression after rule execution and before `# noqa`
  handling, base-relative and independent of the working directory. The
  repeatable `--per-file-ignores "GLOB:CODE[,CODE]"` flag **replaces** the file's
  whole table (contract C2); a malformed value exits `2`. `config show` reports
  the key with its origin.
- **New `flakeforge init` bootstrap command.** `flakeforge init [DIR]` writes a
  fully commented `DIR/flakeforge.toml` with every config key at its default (one
  comment line per key), and `flakeforge init --pyproject [DIR]` appends the same
  body under a `[tool.flakeforge]` table in `DIR/pyproject.toml` instead —
  creating that file when absent and otherwise preserving its existing bytes
  verbatim before a separating blank line. The template is rendered from a text
  template with no TOML-writer dependency (contract C8) and is kept in lockstep
  with the config schema by a test. `init` does not take the shared
  config-selection or path-anchor flags, because it writes a new config rather
  than resolving one. It never overwrites or duplicates (no `--force`): it exits
  `2` when the target `flakeforge.toml` exists, when `pyproject.toml` already
  defines `[tool.flakeforge]` or the legacy `[tool.flake8_lint]` (or is not valid
  TOML, in `--pyproject` mode), or when a same-directory file of the other surface
  would shadow the one being written (contract C3). On success it prints the
  written path and exits `0`.
- **New `flakeforge rules` introspection command.** It lists every rule in the
  resolved registry — code, short description, provider origin, and whether the
  rule is enabled under the effective `select` / `ignore` — without linting. It
  shares the same parent argument parser as `check`, resolves config and the rule
  registry identically (project-local `rule_modules` and installed
  `flakeforge.rules` providers included, invalid config exits `2`, warnings on
  stderr), and reuses the engine's own enablement logic rather than
  reimplementing selector matching. Each rule's origin is `builtin`,
  `rule_module:<name>`, or `entry_point:<dist>`; rows are ordered by code.
  `--output-format json` emits a stable, `sort_keys` document; only `text` and
  `json` render the report, while the `github` / `sarif` annotation formats are
  rejected (exit `2`). To support this, `RuleRegistration` gained an additive,
  defaulted `origin` field stamped by `resolve_registry` per provider loading
  phase — custom providers registering rules through `RuleRegistry.register`
  need no change.
- **New `flakeforge config show` introspection command.** It prints the fully
  resolved, validated configuration and where each value came from, without
  linting. It shares one parent argument parser with `check`, so it accepts the
  same config-selection (`--config` / `--no-config`), path-anchor and override
  flags and reports exactly what `check` would use — same discovery, same
  selector validation against the resolved registry (invalid config exits `2`),
  same legacy/shadow warnings on stderr. The report names the source file and
  section (or `defaults`), the discovery anchor, and `base_dir`, then lists every
  config key with its effective value and its origin (`default` / `file` / `cli`);
  a key set by both the file and the CLI is attributed to the CLI, the winning
  source. Origin attribution lives in a `flakeforge.config` helper and does not
  touch `LintConfig` equality. `--output-format json` emits a stable,
  `sort_keys` document; only `text` and `json` render the report, while the
  `github` / `sarif` annotation formats are rejected (exit `2`). `output_format`
  is itself a reported key, so a file's `output_format = "github"` shows up as
  data with origin `file` while the report still renders as text.
- **New `--statistics` per-code count summary.** `flakeforge check --statistics`
  (TOML key `statistics`, default `false`) appends a per-code summary: in `text`
  it prints aligned `code  count  description` rows (sorted by code) after the
  findings; in `json` it adds an additive `statistics` object mapping each
  violated code to `{count, description}` without touching any existing key or
  the `schema_version`. A clean run omits the `text` block and emits an empty
  `json` object. The `github` and `sarif` formats ignore the flag and warn on
  stderr. `--statistics`/`--no-statistics` override the file value in either
  direction (`argparse.BooleanOptionalAction`), and the flag never changes the
  exit code.
- **New `github` and `sarif` CI output formats.** `flakeforge check
  --output-format github` prints one GitHub Actions `::error` workflow command
  per violation (workflow-command escaped, 1-based columns) so findings render as
  inline PR annotations; a clean run prints nothing. `--output-format sarif`
  emits a SARIF 2.1.0 document (built with the `json` stdlib only) that
  `github/codeql-action/upload-sarif` ingests: it lists every registered rule
  under `tool.driver.rules` and reports each violation with a `base_dir`-relative
  `artifactLocation.uri` and a 1-based `region`. Both formats keep the frozen
  exit codes and the deterministic violation ordering, and are valid on an empty
  result. `flakeforge.api.LintResult` gains an additive `registered_rules` field
  (empty by default) that `lint_paths()` populates for the SARIF formatter.
- **JSON output now carries `"schema_version": 1`.** The `flakeforge check
  --output-format json` document gains a top-level `schema_version` integer so
  consumers can detect incompatible shape changes; all existing keys (`ok`,
  `files_checked`, `violations`) are unchanged. Output formats now live in one
  `flakeforge.api.FORMATTERS` table that drives both the CLI `--output-format`
  choices and config validation.
- **Config path patterns must be relative.** An absolute pattern in a config
  file's `include`, `exclude`, `noqa_allowed`, or `noqa_forbidden` (a POSIX
  `/etc` or a Windows drive/UNC path) is now a config error (CLI exit `2`) naming
  the file/section, the key, and the offending pattern — it would otherwise
  silently escape the config's own directory. This applies to both config
  surfaces and to the lenient legacy `[tool.flake8_lint]` section, since it is a
  value error rather than an unknown key. Relative patterns, including `..`
  segments such as `include = ["../src"]`, stay supported. CLI `--include` /
  `--exclude` values are operator input and may still be absolute.
- **Displayed file paths are now base-relative.** Violation display names are
  relative to the config file's directory (`base_dir`) whenever the file sits
  under it, whatever the current working directory — so a `--config` run prints
  identical, base-relative paths (e.g. `pkg/m.py:3:0: ...`) from any cwd instead
  of leaking a longer ancestor-relative or absolute path.
- **Unknown config keys are now an error.** `flakeforge.toml` and
  `pyproject.toml [tool.flakeforge]` share one strict schema: an unrecognised key
  raises a config error (CLI exit `2`) naming the file/section, the key, and a
  did-you-mean hint (e.g. `flakeforge.toml: unknown key 'exlude' (did you mean
  'exclude'?)`); value type errors carry the same prefix. `flakeforge.toml` may
  also be written as a `[tool.flakeforge]` wrapper table (but not mixed with flat
  keys). The deprecated `[tool.flake8_lint]` section stays lenient — an unknown key
  there is only a warning. Configs valid before this change load unchanged.
- **Renamed the project from `flake8-lint` to `flakeforge`** (package, CLI command, canonical
  config section, custom-rule entry-point group). The name was easy to mistake for `flake8`
  itself rather than an extension of it; renaming now, before wider adoption, keeps the cost
  low. `[tool.flake8_lint]` is accepted as a deprecated fallback for `[tool.flakeforge]`, the
  same way `[tool.flake8_lint_tests]` was accepted before this rename (and is now retired).
- Implement `X003` as a circular-import rule; the code is no longer reserved/disabled.
  Projects that previously carried `ignore = ["X003"]` now opt in by removing it.
- Add `ModuleImport`, `ModuleLocation` and `ModuleImportGraph` import-graph helpers to `flakeforge.rules`.
- Add built-in `X013` (require `subprocess.Popen`/`socket.socket` to be used as a context manager).
- Add built-in `X014` (enforce tracked `TODO`/`FIXME` comment metadata).
- `flakeforge check <dir>` now discovers config from the target path (the discovery anchor)
  instead of the current directory; `discovery_anchor()` is exported for API callers.
- `flakeforge check` now exits `2` with `flakeforge: path does not exist: ...` when a path
  argument does not exist (previously it reported "Checked 0 file(s)" and exited `0`).
- Project-local `rule_modules` now load from the target project's resolved config directory:
  `resolve_registry()` gains a `project_root` parameter that is prepended to `sys.path` only
  while those modules import, then restored (even on failure). A standalone `flakeforge` can
  now lint a project that lists `rule_modules` without that project being installed. This
  executes the target repository's rule-module code, so `--no-config` loads no project rule
  modules and never places the target directory on `sys.path`; see the "Trust boundary" note
  in `docs/custom-rules.md`.

## 1.0.0

- Extract existing AST lint rules into reusable `flake8-lint` package.
- Add standalone `flake8-lint check` CLI.
- Support `pyproject.toml` and `flake8_lint.toml` configuration.
- Preserve X001-X012 codes and reserved X003.
- Add missing X012 test coverage.
- Add configurable `# noqa` policy.
- Add thin Flake8 adapter.
- Add explicit pytest helper without automatic repository scanning.
- Add project-local custom rule modules.
- Add installed custom-rule provider entry points.
- Add custom-rule authoring documentation.
