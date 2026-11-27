# flakeforge examples

Each directory is a self-contained fixture project that demonstrates one facet
of configuration discovery, rule selection, `# noqa` handling, or the
custom-rule extension convention. Every directory ships an `expected.toml`
manifest describing the exact violation codes it should produce; that manifest
is read by `tests/test_examples.py` (it is **not** consumed by
`flakeforge.config`). Every directory also ships an `expected_telemetry.json`
sidecar naming the expected fault-telemetry counts (`findings`, `by_rule`);
that one is read by `tests/test_telemetry_examples.py`, which re-runs the same
scenario with `--telemetry-sink`/`--telemetry-summary` and diffs the resulting
summary document against it.

Run any scenario by hand from the repository root, for example:

```bash
cd src/examples/canonical_flakeforge_toml && flakeforge check .
```

| Scenario                                              | What it proves                                                                                 | Run by hand                                                                 |
|-------------------------------------------------------|------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| [`canonical_flakeforge_toml`](canonical_flakeforge_toml) | The canonical happy path: a `flakeforge.toml` `select` narrows to a single code (`X002`).       | `cd src/examples/canonical_flakeforge_toml && flakeforge check .`         |
| [`legacy_pyproject_section`](legacy_pyproject_section) | The deprecated `[tool.flake8_lint]` section still loads and emits a deprecation warning (`X001`). | `cd src/examples/legacy_pyproject_section && flakeforge check .`           |
| [`pyproject_vs_toml_precedence`](pyproject_vs_toml_precedence) | A sibling `flakeforge.toml` wins over `[tool.flakeforge]` in `pyproject.toml` (`X002`, not `X001`). | `cd src/examples/pyproject_vs_toml_precedence && flakeforge check .`       |
| [`custom_rule_module`](custom_rule_module)            | A project-local `USERNNN` rule module (`USER001`) loaded via `rule_modules`.                     | see note below                                                              |
| [`noqa_path_policy`](noqa_path_policy)                | `noqa_allowed` honours `# noqa` only under whitelisted paths (`X002` fires in `forbidden/` only). | `cd src/examples/noqa_path_policy && flakeforge check allowed forbidden`   |
| [`cli_only`](cli_only)                                | A CLI-only project (no pytest wiring) that flags a missing docstring (`X005`).                   | `cd src/examples/cli_only && flakeforge check .`                           |
| [`pytest_only`](pytest_only)                          | A pytest-only project using `assert_lint_clean` + `load_config` (`X009`).                        | `cd src/examples/pytest_only && python -m pytest test_lint.py`              |
| [`both_modes_parity`](both_modes_parity)              | CLI and pytest paths see the identical violation set (`X001`, `X002`).                           | `cd src/examples/both_modes_parity && flakeforge check .`                  |

## Note on `custom_rule_module`

`custom_rule_module` loads a project-local rule module (`proj_rules.lint_rules`)
that must be importable. Two equivalent ways to run it:

- Standalone console script (needs the directory on the import path):

  ```bash
  cd src/examples/custom_rule_module && PYTHONPATH=. flakeforge check .
  ```

- Module invocation (works out of the box, because `python -m` inserts the
  current directory as `sys.path[0]`):

  ```bash
  cd src/examples/custom_rule_module && python -m flakeforge check .
  ```
