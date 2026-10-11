# flakeforge

Reusable AST-based Python lint rules with a standalone CLI, a thin Flake8 adapter, explicit pytest helpers, and first-class custom-rule support.

## What it is

`flakeforge` packages the copied `flake8_project_rules` behavior as a reusable distribution:

- standalone `flakeforge check`
- shared Python API
- built-in X001–X015 rules, all enabled by default
- project-local and installed custom rules
- opt-in pytest helper
- thin Flake8 integration over the same core engine

## Installation

```bash
pip install flakeforge
```

For local development:

```bash
pip install -e .[dev]
```

## Basic CLI usage

```bash
flakeforge --version
flakeforge check
flakeforge check .
flakeforge check src tests
flakeforge check path/to/file.py
flakeforge check --select X002 --ignore X011 src
flakeforge check --output-format json .
flakeforge check --rule-module my_project.lint_rules .
flakeforge check --no-rule-plugins .
```

Exit codes:

- `0` = completed, no violations
- `1` = completed, violations found
- `2` = invalid config, invalid invocation, or tool failure

## Inspecting the effective config

`flakeforge config show` prints the fully resolved, validated configuration and
where each value came from, without linting anything. It accepts the same
config-selection, path-anchor and override flags as `check` (`--config` /
`--no-config`, positional paths, `--select`, `--ignore`, and so on), so it reports
exactly what `check` would use:

```bash
flakeforge config show                       # discovered config, anchored on cwd
flakeforge config show path/to/proj          # anchored on the target, run from anywhere
flakeforge config show --no-config .          # what the built-in defaults are
flakeforge config show --select X002 src      # see a CLI override take effect
flakeforge config show --output-format json . # stable, sort_keys JSON document
```

The report names the source file and section (or `defaults`), the discovery
anchor, and `base_dir`, then lists every config key with its effective value and
its origin — `default`, `file`, or `cli`. Legacy-section and shadowed-file
warnings, and an invalid config (exit `2`), surface exactly as they do for
`check`. Only `text` (default) and `json` render the report; `github` / `sarif`
are annotation formats for findings and are rejected here (exit `2`). Note that
`output_format` is itself a reported key: a file's `output_format = "github"`
shows up as data with origin `file` while the report still renders as text.

> Both `config show` and `rules` resolve the registry the way `check` does, so they import the
> project's `rule_modules`. Use `--no-config` on untrusted checkouts; see the
> [trust boundary](docs/custom-rules.md#trust-boundary).

## Listing the resolved rules

`flakeforge rules` lists every rule in the resolved registry — its code, short
description, provider origin, and whether it is enabled under the effective
`select` / `ignore` — without linting anything. It accepts the same
config-selection, path-anchor and override flags as `check`, and resolves the
registry the same way (project-local `rule_modules` and installed
`flakeforge.rules` providers included), so what it lists is what `check` would
run:

```bash
flakeforge rules                       # every registered rule, enabled state included
flakeforge rules --select X002         # see which rules a selector leaves enabled
flakeforge rules --no-rule-plugins .   # hide installed entry-point providers
flakeforge rules --no-config .         # built-ins only, ignoring project rule modules
flakeforge rules --output-format json  # stable, sort_keys JSON document
```

Each rule's origin is one of `builtin`, `rule_module:<name>` for a project-local
module, or `entry_point:<dist>` for an installed provider. Rows are ordered by
code. Only `text` (default) and `json` render the report; the `github` / `sarif`
annotation formats are rejected (exit `2`), and an invalid config exits `2`,
exactly as for `check`.

## Writing a starter config

`flakeforge init` writes a fully commented starter config with every key at its
default, so you can delete what you do not need and edit the rest:

```bash
flakeforge init                  # write ./flakeforge.toml
flakeforge init path/to/proj     # write path/to/proj/flakeforge.toml
flakeforge init --pyproject      # append a [tool.flakeforge] table to ./pyproject.toml
```

Unlike `check`, `config show`, and `rules`, `init` does not take the
config-selection or path-anchor flags: it *writes* a fresh config rather than
resolving an existing one, so `--config`, `--no-config`, and the override flags
would have nothing to act on.

It never overwrites or duplicates (there is no `--force`): it exits `2` with a
message when the target `flakeforge.toml` already exists, when `pyproject.toml`
already defines `[tool.flakeforge]` or the legacy `[tool.flake8_lint]`, or when a
same-directory file of the other surface would shadow the one being written (a
`flakeforge.toml` outranks a `pyproject.toml` section, C3). `--pyproject` creates
`pyproject.toml` if it is absent and otherwise preserves the existing content
byte-for-byte, appending the table after a separating blank line; a `pyproject.toml`
that is not valid TOML is refused rather than appended to. On success it prints the
written path and exits `0`.

## Standalone CLI

`flakeforge` runs as a fully standalone linter: point it at any target and, optionally,
pass a single TOML file that carries the same parameters as the command line. It does not
need to share the target project's virtualenv, and it does not need to be run from the
project root.

### Config discovery is anchored on the target

Config discovery walks upward from the *discovery anchor* — the lint target — not from the
current working directory, so `flakeforge check path/to/proj` honours
`path/to/proj/flakeforge.toml` even when it is invoked from elsewhere:

- **C3 — config-file selection.** `--config PATH` wins; otherwise `--no-config` (built-in
  defaults only); otherwise discovery. Within each directory, nearest first:
  `flakeforge.toml` > `pyproject.toml [tool.flakeforge]` > `pyproject.toml
  [tool.flake8_lint]` (deprecated). The first directory with a match wins.
- **C4 — discovery anchor.** No path arguments → the current directory. One path → that
  directory (a file counts as its parent directory). Several paths → their deepest common
  ancestor directory. Linting several unrelated projects in one run is out of scope.

A path argument that does not exist is an invalid invocation (exit code `2`).

### A config file that carries the CLI parameters

Every `check` flag has a TOML key and every TOML key has a flag, so one config file can
stand in for a long command line. For example, this CI profile:

```toml
# ci.toml
output_format = "json"
rule_plugins = false
select = ["X001", "X005"]
```

is equivalent to the flags:

```bash
flakeforge check --output-format json --no-rule-plugins --select X001,X005 src
```

Pass the file explicitly with `--config`, which wins over discovery:

```bash
flakeforge check --config ci.toml src
```

The full key/flag reference lives in [Configuration](#configuration).

## Configuration

`flakeforge` reads its settings from a single config file. Both surfaces —
`pyproject.toml [tool.flakeforge]` and a standalone `flakeforge.toml` — share
**one schema** (contract C5): the same keys, parsed by the same code, so a
snippet can be copied verbatim between them. A `flakeforge.toml` may use either
flat top-level keys or a single `[tool.flakeforge]` wrapper table (not both).

```toml
# pyproject.toml
[tool.flakeforge]
include = []
exclude = []
select = []
ignore = []
allow_noqa = true
noqa_allowed = []
noqa_forbidden = []
per_file_ignores = {}
rule_modules = []
rule_plugins = true
output_format = "text"
```

```toml
# flakeforge.toml (same keys, no [tool.flakeforge] header needed)
include = []
exclude = []
select = []
ignore = []
allow_noqa = true
noqa_allowed = []
noqa_forbidden = []
per_file_ignores = {}
rule_modules = []
rule_plugins = true
output_format = "text"
```

`include` is a whitelist. `exclude` is a blacklist and wins. `select` is a
whitelist. `ignore` is a blacklist and wins. `noqa_allowed` and `noqa_forbidden`
are path filters for files allowed to use `# noqa`; `noqa_forbidden` wins.
Unknown keys are a config error (exit code `2`) with a "did you mean" hint;
the deprecated `[tool.flake8_lint]` section is the one exception, where an
unknown key is only a warning.

### Key reference

| CLI                                    | TOML key                         | Type / default                     |
|----------------------------------------|----------------------------------|------------------------------------|
| `--select`                             | `select`                         | list[str] / `[]`                   |
| `--ignore`                             | `ignore`                         | list[str] / `[]`                   |
| `--include`                            | `include`                        | list[str] / `[]`                   |
| `--exclude`                            | `exclude`                        | list[str] / `[]`                   |
| `--noqa` / `--no-noqa`                 | `allow_noqa`                     | bool / `true`                      |
| `--rule-module`                        | `rule_modules`                   | list[str] / `[]`                   |
| `--rule-plugins` / `--no-rule-plugins` | `rule_plugins`                   | bool / `true`                      |
| `--output-format`                      | `output_format`                  | `"text"` / `"json"` / `"github"` / `"sarif"`, default `"text"` |
| `--statistics` / `--no-statistics`     | `statistics`                     | bool / `false`                     |
| *(file-only, no CLI flag)*             | `apply_baseline`                 | bool / `false`                     |
| `--per-file-ignores GLOB:CODE[,CODE]`  | `per_file_ignores`               | table `{glob = [codes]}` / `{}`    |
| `--baseline PATH`                      | `baseline`                       | str path / `""` (none)             |
| `--write-baseline PATH`                | *(CLI-only, no TOML key)*        | write current fingerprints, exit `0` |
| `--no-config`                          | *(CLI-only, no TOML key)*        | flag; skip discovery, defaults + CLI |
| *(file-only, no CLI flag)*             | `noqa_allowed`, `noqa_forbidden` | list[str] / `[]`                   |

`per_file_ignores` maps a path glob (resolved against the config file's
directory like `include`/`exclude`, C6; absolute globs are a config error) to
rule-code prefixes skipped for matching files — e.g.
`per_file_ignores = { "tests/**" = ["X002"], "scripts/*.py" = ["X0"] }`. The
codes are validated like `ignore` (an unknown selector exits `2`) and applied by
the engine after rule execution and before `# noqa`. The repeatable
`--per-file-ignores "GLOB:CODE[,CODE]"` flag **replaces** the file's whole table
(C2); a malformed value exits `2`.

### Baseline (incremental adoption)

A **baseline** lets an existing codebase adopt `flakeforge` without fixing every
violation on day one: it records the violations you accept today so later runs
fail only on *new* ones. The workflow is **write once, commit, shrink over time**:

```bash
# 1. Record every current violation and commit the file.
flakeforge check --write-baseline flakeforge-baseline.json src
git add flakeforge-baseline.json

# 2. From then on, gate on new violations only (exit 1 only for new ones).
flakeforge check --baseline flakeforge-baseline.json src
```

Each recorded entry is a fingerprint —
`sha256(code + relative path + normalized source line text)` plus an occurrence
index for identical lines. Because it hashes the *line text*, not the line
*number*, a baselined violation stays suppressed when unrelated lines are added
above it, but **re-surfaces the moment you edit the flagged line** — so the
baseline can only shrink, never silently absorb a regression. `--write-baseline`
always exits `0` (it records, it does not gate) and writes the full current set,
ignoring any `--baseline` in effect.

Set it in config with `baseline = "flakeforge-baseline.json"` and
`apply_baseline = true` (the path is relative to the config file, C6; absolute
paths are rejected). Baseline application is opt-in and defaults to `false`, so
configuring a baseline alone does not hide existing violations. When a recorded
entry no longer matches anything it is reported as **fixed** — a count in text
output and a list under the additive `baseline` key in JSON — so you know what to
prune. A missing or malformed baseline file exits `2` when it is applied.

`noqa_allowed` and `noqa_forbidden` are **file-only**: they have no matching CLI
flag and can be set only in a config file. Boolean flags use
`argparse.BooleanOptionalAction`, so `--noqa`/`--no-noqa` and
`--rule-plugins`/`--no-rule-plugins` can override a file's value in either
direction. An unknown `output_format` is a config error (exit code `2`).

### Value precedence (C2)

For every setting: an **explicit CLI flag > the selected config file > the
built-in default**. The list flags `--select`, `--ignore`, `--include`, and
`--exclude` **replace** the file's value; `--rule-module` **appends** to it.
`--include`/`--exclude` are repeatable and comma-splittable (like `--select`).

### Config-file selection (C3)

`--config PATH` > `--no-config` (defaults only) > discovery. Discovery walks
upward from the *discovery anchor* — the lint target, not the process cwd
(C4) — and in each directory picks, nearest first: `flakeforge.toml` >
`pyproject.toml [tool.flakeforge]` > `pyproject.toml [tool.flake8_lint]`
(deprecated). The first directory with a match wins, and a `pyproject.toml`
without any recognised section does **not** stop the upward search.

If `flakeforge.toml` and a `pyproject.toml` section sit in the **same
directory**, `flakeforge.toml` wins and a single warning names the shadowed
section. That shadow warning is discovery-only: pointing `--config` straight at
`flakeforge.toml` selects it silently. Passing both `--config` and `--no-config`
is rejected with exit code `2`.

`--no-config` skips discovery entirely: only built-in defaults and CLI flags
apply, and no project-local `rule_modules` load (installed entry-point providers
still follow `--rule-plugins`; `--rule-module` stays an explicit opt-in).

Legacy `[tool.flake8_lint]` is still accepted as a migration fallback when the
canonical section is absent — the same way `[tool.flake8_lint_tests]` was
accepted as a fallback for `[tool.flake8_lint]` before this rename (that older
fallback is now retired). The CLI warns:

```text
[tool.flake8_lint] is deprecated; rename it to [tool.flakeforge].
```

### Path patterns (C6)

The path-pattern keys — `include`, `exclude`, `noqa_allowed`, `noqa_forbidden`,
and the `per_file_ignores` globs — resolve against the **directory of the config
file** they appear in, not the
current working directory. With `--no-config` they resolve against the discovery
anchor. Absolute patterns are **rejected** with exit code `2`; relative
patterns, including ones that climb out with `..` (e.g. `../src`), are accepted.
Display names in output are shown relative to that base directory when a file
sits under it, and otherwise shown as given.

### When to pick which file

- **`pyproject.toml [tool.flakeforge]`** — flakeforge is one of several tools the
  project already configures in `pyproject.toml`. This is the default choice for
  a tool-mode setup.
- **`flakeforge.toml`** — a dedicated, standalone config, or when you want it to
  win over an inherited `pyproject.toml` section in the same tree.
- **`--config ci.toml`** — a throwaway or CI-specific profile that should apply
  regardless of discovery; it wins over any discovered file.
- **`--no-config`** — a hermetic run driven entirely by CLI flags, ignoring every
  on-disk config and every project-local rule module.

## Built-in rules

- `X001` bare `except:`
- `X002` `except Exception:`
- `X003` circular imports between project modules
- `X004` silently swallowed exceptions
- `X005` missing or malformed docstrings
- `X006` local imports inside functions or classes
- `X007` value-returning functions without return annotations
- `X008` explicit `-> None`
- `X009` old `%` string formatting
- `X010` suppressed `ImportError` / `ModuleNotFoundError`
- `X011` `Type | None` instead of `Optional[Type]`
- `X012` non-`None` PEP 604 unions like `Type1 | Type2` instead of `Union[...]`
- `X013` `subprocess.Popen`/`socket.socket` not used as a context manager
- `X014` malformed or untracked `TODO`/`FIXME` comments
- `X015` unused `# noqa` directives (a `# noqa` that suppressed nothing, or one naming an unknown code)

### `X003` circular imports

`X003` resolves each module's runtime imports against its import root - the first directory above the module that is not a package - and reports any import that takes part in a cycle leading back to the module being checked. Every module in a cycle is flagged, each at its own offending import line, and the message names the full chain:

```text
src/pkg/alpha.py:3:0: X003 Circular import detected: pkg.alpha -> pkg.beta -> pkg.alpha; ...
```

These are deliberately *not* treated as cycle edges, because Python does not run them while first importing the module:

- imports inside a function body, which is the standard way to break a cycle
- imports guarded by `if TYPE_CHECKING:`
- targets that do not resolve to a file under the import root, so the standard library and third-party packages are never traversed

## `# noqa` semantics

Supported forms:

```python
# noqa
# noqa: X002
# noqa: X001, X002
# noqa: ACME001
```

- bare `# noqa` suppresses package violations on that line
- code-qualified markers suppress only matching codes or prefixes
- `allow_noqa = false` disables suppression globally
- `noqa_allowed` restricts suppression to matching file paths when non-empty
- `noqa_forbidden` always blocks suppression for matching file paths

If source text is unavailable, the engine still reports violations but cannot apply source-based `# noqa` suppression.

## Custom rules

Minimal project-local registration:

```python
# my_project/lint_rules.py
import ast
from flakeforge import RuleContext, RuleRegistry, RuleViolation


class NoPrintRule:
    code = "ACME001"
    description = "Do not call print() in production code."

    def check(self, context: RuleContext):
        for node in ast.walk(context.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                yield RuleViolation(
                    context.filename,
                    node.lineno,
                    node.col_offset,
                    self.code,
                    self.description,
                )


def register_rules(registry: RuleRegistry) -> None:
    registry.register(NoPrintRule(), provider="my_project.lint_rules")
```

```toml
[tool.flakeforge]
rule_modules = ["my_project.lint_rules"]
```

Reusable provider packages can also register through the `flakeforge.rules` Python entry-point group. See [docs/custom-rules.md](docs/custom-rules.md).

## Flake8 integration

`flakeforge` registers a Flake8 AST plugin entry point. The adapter is intentionally thin:

- built-in diagnostics come from the same shared engine used by the CLI/API
- Flake8 selection and `--disable-noqa` map into the shared engine
- installed `flakeforge.rules` providers remain authoritative for the standalone CLI/API
- third-party packages that need native Flake8 discovery should also expose their own Flake8 entry points

## pytest integration

Pytest integration is explicit:

```python
from flakeforge.testing import assert_lint_clean


def test_project_lint() -> None:
    assert_lint_clean("src", "tests")
```

Installing `flakeforge` alone does not auto-run repository lint during ordinary `pytest`.

## CI recommendation

Prefer separate steps or jobs:

```bash
flakeforge check .
pytest
```

That keeps unit and integration tests running even when lint finds violations.

## CI output formats

`--output-format` accepts `text` (default), `json`, `github`, and `sarif`. The
last two are meant for CI systems. Exit codes are unchanged (`0` clean, `1`
violations, `2` invalid config or invocation), so a failing lint still fails the
job whatever the format.

### GitHub Actions annotations (`github`)

The `github` format prints one `::error` workflow command per violation, so
findings show up as inline annotations on the pull request:

```yaml
# .github/workflows/lint.yml
name: lint
on: [push, pull_request]
jobs:
  flakeforge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install flakeforge
      - run: flakeforge check . --output-format github
```

A clean run prints nothing and exits `0`; a run with violations annotates each
line and exits `1`, failing the job.

### Code scanning with SARIF (`sarif`)

The `sarif` format emits a SARIF 2.1.0 document that GitHub code scanning ingests
via `github/codeql-action/upload-sarif`. Redirect it to a file and upload it even
when the lint step fails, so annotations still appear:

```yaml
# .github/workflows/code-scanning.yml
name: code-scanning
on: [push, pull_request]
jobs:
  flakeforge-sarif:
    runs-on: ubuntu-latest
    permissions:
      security-events: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install flakeforge
      - name: Run flakeforge
        run: flakeforge check . --output-format sarif > flakeforge.sarif
        continue-on-error: true
      - name: Upload SARIF
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: flakeforge.sarif
```

The document lists every registered rule under `tool.driver.rules` and reports
each violation with a `base_dir`-relative `artifactLocation.uri` and a 1-based
`region`.

### Per-code statistics (`--statistics`)

`--statistics` (TOML key `statistics`, default `false`) appends a per-code count
summary. In `text` it follows the violation lines as aligned
`code  count  description` rows (sorted by code); in `json` it adds a
`statistics` object mapping each violated code to `{"count", "description"}`
without changing any existing key or the `schema_version`. A clean run has
nothing to summarise, so `text` omits the block and `json` emits an empty
object. The `github` and `sarif` formats ignore `--statistics` and print a
warning on stderr. The flag never changes the exit code.

```bash
flakeforge check . --statistics                       # text summary after findings
flakeforge check . --output-format json --statistics  # adds a "statistics" object
```

## Renamed from flake8-lint

This project was initially released as `flake8-lint` and was later renamed to `flakeforge` to
avoid it reading as "flake8 itself" rather than an extension of it.
If you depend on an older release or a pre-rename commit:

- the import package is `flakeforge` (was `flake8_lint`)
- the console script is `flakeforge` (was `flake8-lint`)
- the canonical config section is `[tool.flakeforge]` / `flakeforge.toml` (was `[tool.flake8_lint]` /
  `flake8_lint.toml`) — `[tool.flake8_lint]` is still accepted as a deprecated fallback, same as
  any other config migration (see the precedence list above)
- the custom-rule entry-point group is `flakeforge.rules` (was `flake8_lint.rules`)

No rule code, rule behaviour, or public API shape changed - only names.

## Migration from the copied implementation

Before:

```text
src/flake8_project_rules/
tests/flake8_lint/config.py
tests/flake8_lint/test_plugin_direct.py
tests/flake8_lint/test_project_lint.py
tests/flake8_lint/samples/
```

Add dependency:

```text
flakeforge==1.0.0
```

Then:

- remove `src/flake8_project_rules/`
- remove copied package-implementation tests now owned by `flakeforge`
- remove `test_project_lint.py` unless pytest should stay an explicit lint gate
- rename the legacy config section to `[tool.flakeforge]`
- change CI from implicit pytest lint gating to:

```bash
flakeforge check .
pytest
```

- update direct imports from `flake8_project_rules` to public `flakeforge` APIs where needed

## Canonical migrated configuration example

`pyproject.toml`:

```toml
[tool.flakeforge]

include = []

exclude = [
    "tests/flake8_lint/samples/",
    "src/aegis_swr/openclaude/grpc/client.py",
    "src/aegis_swr/openclaude/tool.py",
    "src/aegis_swr/openclaude/grpc/proto/",
    "src/aegis_swr/openhands/client.py",
    "src/aegis_swr/openhands/tool.py",
    "src/aegis_swr/claude_code/tool.py",
]

select = [
    "X001",
    "X002",
    "X003",
    "X004",
    "X005",
    "X006",
    "X007",
    "X008",
    "X009",
    "X010",
    "X011",
    "X012",
    "X013",
    "X014",
    "X015",
]

ignore = []

allow_noqa = true

noqa_allowed = [
    "src/aegis_swr/core/events.py",
]

noqa_forbidden = []

rule_modules = []
```

Equivalent `flakeforge.toml`:

```toml
include = []

exclude = [
    "tests/flake8_lint/samples/",
    "src/aegis_swr/openclaude/grpc/client.py",
    "src/aegis_swr/openclaude/tool.py",
    "src/aegis_swr/openclaude/grpc/proto/",
    "src/aegis_swr/openhands/client.py",
    "src/aegis_swr/openhands/tool.py",
    "src/aegis_swr/claude_code/tool.py",
]

select = [
    "X001",
    "X002",
    "X003",
    "X004",
    "X005",
    "X006",
    "X007",
    "X008",
    "X009",
    "X010",
    "X011",
    "X012",
    "X013",
    "X014",
    "X015",
]

ignore = []
allow_noqa = true
noqa_allowed = ["src/aegis_swr/core/events.py"]
noqa_forbidden = []
rule_modules = []
```

## Development / build

```bash
pytest --cov=flakeforge --cov-report=term-missing
ruff check .
python -m build
```

Additional documentation:

- [docs/custom-rules.md](docs/custom-rules.md)
- [docs/architecture.md](docs/architecture.md)
