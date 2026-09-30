# Architecture

`flakeforge` keeps the production rule engine independent from CLI argument parsing, project discovery policy, Flake8 internals, and pytest integration details.

```text
                  +---------------------------+
                  |   rules + registry        |
                  |   shared execution core   |
                  +-------------+-------------+
                                |
        +-----------------------+------------------------+
        |                       |                        |
        v                       v                        v
  config + discovery       standalone API/CLI      Flake8 + pytest
  path policy              orchestration           thin integrations
```

## Core rules / registry

- `flakeforge.rules` defines the built-in X001–X015 rules (only the rule functions, in ascending code order).
- `flakeforge.rule_helpers` holds the helper functions, classes, and constants those rules share.
- `flakeforge.catalog` wires the built-ins into the registry via `builtin_registrations()`.
- `flakeforge.registry` owns registration, duplicate detection, rule-code validation, and provider loading.
- Built-in rules are registered through the same registry mechanism used by custom rules.
- `X003` detects circular imports by resolving each module's runtime imports against the import root.

That shared registration path matters because extension behavior should exercise the real engine path instead of a separate bolt-on custom-rule system.

## Config

`flakeforge.config` owns:

- typed config parsing
- the *discovery anchor* (`discovery_anchor()`): the directory config discovery walks
  upward from — the lint target, not the process working directory (contract C4). No
  path arguments resolve to the current directory, one path to that directory (a file
  counts as its parent), and several paths to their deepest common ancestor.
- discovery precedence for `--config`, `flakeforge.toml`, canonical `pyproject.toml`, legacy
  `pyproject.toml`, and defaults (contract C3). Nearest directory wins; a `pyproject.toml`
  with no recognised section does not stop the upward search. When a `flakeforge.toml` and a
  `pyproject.toml` section share a directory, `flakeforge.toml` wins and one shadow warning
  names the shadowed section — but only during discovery, never for an explicit `--config`.
- one strict schema (`_CONFIG_SCHEMA`) shared by `flakeforge.toml` (flat or `[tool.flakeforge]`
  wrapped) and `pyproject.toml [tool.flakeforge]` (contract C5): unknown keys are an error
  (exit `2`) with a did-you-mean hint; the legacy `[tool.flake8_lint]` section stays lenient,
  turning an unknown key into a warning
- config-relative path patterns (contract C6): `include`/`exclude`/`noqa_allowed`/
  `noqa_forbidden` resolve against the config file's directory; absolute patterns are rejected
  (exit `2`), relative `..` patterns are allowed, and display names are base-relative
- `--no-config` isolated mode (`isolated_config()`): defaults plus CLI flags only, with
  path patterns resolved against the anchor and no project-local `rule_modules` loaded
- legacy migration metadata and warnings
- rule-selector validation against the resolved registry

Config parsing does not print warnings directly. Integrations choose whether to surface warnings.

## Custom provider loading

`flakeforge.registry.resolve_registry()` builds one resolved registry from:

1. built-in rules
2. configured project-local `rule_modules`
3. installed `flakeforge.rules` entry points, unless disabled

Provider loading is deterministic. Duplicate codes and invalid codes fail fast. Import and provider registration failures are surfaced as tool/configuration errors instead of being silently ignored.

Project-local `rule_modules` load with the resolved config `base_dir` (passed as `resolve_registry(project_root=...)`) placed first on `sys.path`, only while those modules import; the previous `sys.path` is restored afterward even if loading fails. This lets a standalone run import a target project's rule modules without the project being installed, and therefore executes that project's code — a trust boundary documented in [custom-rules.md](custom-rules.md#trust-boundary). `--no-config` passes no `project_root`, so no project rule modules load and the target directory is never added to `sys.path`. Entry-point providers are unaffected; they resolve through installed metadata. The `sys.path` window is process-global and not thread-safe.

## Discovery

`flakeforge.discovery` owns:

- recursive Python file traversal
- include/exclude path filtering
- default cache/build/virtualenv directory skipping
- stable ordering and duplicate removal

The discovery layer is intentionally filesystem-focused. It does not parse rule logic or format output. It walks the path arguments as given; the separate *config* discovery anchor (see [Config](#config)) decides which config file governs a run, not which files are traversed.

## API runner

`flakeforge.api` owns the reusable orchestration surface:

- `check_tree()`
- `check_file()`
- `check_source()`
- `lint_paths()`
- structured `RuleViolation` and `LintResult`

The API is the shared execution boundary. It validates config selectors against the resolved registry, runs built-in and custom rules in deterministic order, and applies the common `# noqa` semantics.

## CLI

`flakeforge.cli` is a thin integration over the API:

- parses command-line options
- loads config with precedence rules
- resolves providers
- emits text or JSON output
- maps outcomes to exit codes `0`, `1`, and `2`

The CLI is responsible for warning presentation, not low-level modules.

## Flake8 adapter

`flakeforge.plugin.ProjectRulesPlugin` is a thin AST adapter:

- reuses the shared engine for built-in diagnostics
- maps Flake8 select/ignore and disable-noqa options into engine config
- intentionally keeps installed external provider discovery authoritative in the standalone CLI/API

That boundary avoids over-claiming support for provider-loading behavior that Flake8 itself does not own.

## pytest helper

`flakeforge.testing.assert_lint_clean()` is explicit opt-in:

- it calls the shared API
- it formats failures for assertions
- it does not auto-register linting during ordinary `pytest`

Recommended CI remains separate lint and test steps so test suites can still run when lint finds violations.
