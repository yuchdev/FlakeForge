# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`flakeforge` is a standalone, reusable AST-based Python lint rule engine. It ships four ways to run the same core:
a standalone CLI (`flakeforge check`), a Python API, a thin Flake8 plugin adapter, and an opt-in pytest helper.
Built-in rules are `X001`-`X015` (all enabled by default); projects can add their own codes via project-local
rule modules or installed `flakeforge.rules` entry points.

## Commands

```bash
pip install -e .[dev]                                   # local dev install

pytest                                                    # run tests
pytest --cov=flakeforge --cov-report=term-missing        # run tests with coverage (used in CI)
pytest tests/test_rules.py                                # run a single test file
pytest tests/test_rules.py::test_name -v                  # run a single test

ruff check .                                              # lint (select = E, F, I; line-length 100)
python -m build                                           # build sdist/wheel

flakeforge check .                                       # self-check via the CLI
python -m flakeforge check --select X001,X009,X010 src   # subset self-check (as CI does)
```

CI (`.github/workflows/ci.yml`) runs, per Python 3.11-3.14: pytest+coverage, ruff, a `flakeforge` self-check
subset, then `python -m build`, followed by a separate wheel-smoke job that installs the built wheel into a
fresh venv and exercises the CLI end-to-end (config precedence, exit codes, custom rule modules, entry-point
providers). When changing CLI behavior, config precedence, or exit codes, check that job's script in the
workflow file, since it encodes expected exit codes and output as executable assertions.

## Architecture

The core engine (`rules.py` + `registry.py`) is intentionally isolated from every integration surface. Everything
else (config/discovery, the CLI/API, and the Flake8/pytest adapters) is a thin layer over that shared core:

```
                  rules + registry (shared execution core)
                                |
        +-----------------------+------------------------+
        |                       |                        |
  config + discovery       standalone API/CLI      Flake8 + pytest
  (path policy)             (orchestration)         (thin adapters)
```

- **`registry.py`** — owns rule registration, duplicate/invalid-code detection, and provider loading. Built-in
  rules register through the *same* mechanism as custom rules, so there is no separate bolt-on path for
  extensions. `resolve_registry()` builds one resolved registry from: built-ins → project-local `rule_modules`
  → installed `flakeforge.rules` entry points (unless disabled). Provider loading is deterministic; duplicate
  or malformed codes fail fast rather than being silently dropped.
- **`rules.py`** — *only* the X001-X015 built-in rule `_check_*` functions, kept in ascending code order.
  Every helper function/class/constant they use lives in **`rule_helpers.py`**; the `CallbackRule` adapter and
  the `builtin_registrations()` table live in **`catalog.py`** (a separate module so `rules` ↔ helpers and
  catalog ↔ rules stay acyclic).
- **`config.py`** — typed config parsing and discovery precedence: `--config PATH` > `flakeforge.toml` >
  `pyproject.toml [tool.flakeforge]` > legacy `pyproject.toml [tool.flake8_lint]` > defaults. Does not
  print warnings itself — that's left to callers (CLI decides how to surface them).
- **`discovery.py`** — filesystem-only: recursive traversal, include/exclude filtering, default skip of
  cache/build/venv dirs, stable ordering/dedup. Does not know about rules or output formatting.
- **`api.py`** — the shared orchestration boundary: `check_tree()`, `check_file()`, `check_source()`,
  `lint_paths()`, plus `RuleViolation`/`LintResult`. Validates selectors against the resolved registry, runs
  rules in deterministic order, applies `# noqa` semantics. This is the layer both the CLI and third-party
  integrations should call into.
- **`cli.py`** — thin: arg parsing, config loading, provider resolution, text/JSON output, and mapping outcomes
  to exit codes `0` (clean) / `1` (violations found) / `2` (invalid config/invocation/tool failure). Exit codes
  are a public contract — don't change their meaning.
- **`plugin.py`** (`ProjectRulesPlugin`) — the Flake8 AST adapter. Reuses the shared engine for built-in
  diagnostics and maps Flake8 select/ignore/disable-noqa into engine config. Deliberately does *not* claim to
  auto-discover installed `flakeforge.rules` providers the way the standalone CLI/API does — third-party
  packages needing native Flake8 discovery should expose their own Flake8 entry point.
- **`testing.py`** (`assert_lint_clean`) — explicit pytest opt-in helper over the API. Installing `flakeforge`
  must never cause ordinary `pytest` to auto-run repository-wide lint.

## Conventions specific to this repo

- Never renumber or reuse existing X-codes. `X003` detects circular imports (it was a reserved placeholder until it was implemented).
- Custom rule codes must be uppercase, an alphanumeric prefix, ending in exactly three digits (e.g. `ACME001`);
  duplicates (including collisions with built-in codes) are registry errors, not warnings.
- Adding a built-in rule requires updates in lockstep: `catalog.py` registration, `rules.py` implementation
  (at its code's position; helpers go in `rule_helpers.py`), tests, a
  sample file under `tests/samples/`, and README/docs. Changing the custom-rule extension surface (`RuleContext`,
  `RuleRegistry`, `RuleViolation`, registration contract) requires updating `tests/test_custom_rules.py`.
  `docs/custom-rules.md` documents the full extension contract (`RuleContext`/`RuleViolation` fields, project-local
  vs. installed-provider registration, `# noqa` interaction) in detail — read it before adding or changing rules.
- `# noqa` suppression is engine-owned, not rule-owned: rules just yield violations, and `api.py` decides
  suppression based on `allow_noqa` / `noqa_allowed` / `noqa_forbidden` path policy. Rules must not implement
  their own noqa handling.
- Production code must not import from `tests/`.
