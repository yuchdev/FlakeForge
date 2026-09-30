import json
import textwrap
import tomllib
from importlib.metadata import version

import pytest

from flakeforge.cli import main
from flakeforge.config import CONFIG_KEYS, LintConfig, load_config


def test_cli_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"flakeforge {version('flakeforge')}"


def test_cli_check_json_reports_violation(tmp_path, monkeypatch, capsys) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def handler() -> int:\n"
        '    """Handle a broad exception."""\n'
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        return 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["check", str(sample), "--select", "X002", "--output-format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["files_checked"] == 1
    assert payload["violations"][0]["code"] == "X002"


def test_cli_text_uses_relative_paths_and_exit_zero(tmp_path, monkeypatch, capsys) -> None:
    src = tmp_path / "src"
    src.mkdir()
    sample = src / "sample.py"
    sample.write_text(
        'def documented() -> int:\n    """Return a number."""\n    return 1\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == 0
    assert capsys.readouterr().out.strip() == "Checked 1 file(s); no violations found."


def test_cli_errors_are_reported_on_stderr(capsys) -> None:
    assert main(["check", "--rule-module", "missing.module", "."]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing.module" in captured.err


def test_cli_uses_explicit_config_and_rule_module(tmp_path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_dir = project / "config"
    config_dir.mkdir()
    package = project / "demo_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lint_rules.py").write_text(
        textwrap.dedent(
            """
            from flakeforge import RuleRegistry, RuleViolation

            class LocalRule:
                code = "ORG001"
                description = "Local rule"

                def check(self, context):
                    yield RuleViolation(context.filename, 1, 0, self.code, "local rule")

            def register_rules(registry: RuleRegistry) -> None:
                registry.register(LocalRule(), provider="demo_project.lint_rules")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (config_dir / "flakeforge.toml").write_text(
        'include = ["../src"]\nselect = ["ORG001"]\n',
        encoding="utf-8",
    )
    src = project / "src"
    src.mkdir()
    sample = src / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(project))

    monkeypatch.chdir(project)
    assert (
        main(
            [
                "check",
                "--config",
                "config/flakeforge.toml",
                "--rule-module",
                "demo_project.lint_rules",
                ".",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "src/sample.py:1:0: ORG001 local rule" in captured.out


def test_cli_warns_for_legacy_config_and_invalid_canonical_config(tmp_path, monkeypatch, capsys) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pyproject.toml").write_text(
        '[tool.flake8_lint]\nselect = ["X001", "X999"]\n',
        encoding="utf-8",
    )
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001", "X999"]\n',
        encoding="utf-8",
    )
    sample = 'def documented() -> int:\n    """Return a number."""\n    return 1\n'
    (legacy / "sample.py").write_text(sample, encoding="utf-8")
    (canonical / "sample.py").write_text(sample, encoding="utf-8")

    monkeypatch.chdir(legacy)
    assert main(["check", "sample.py"]) == 0
    legacy_capture = capsys.readouterr()
    assert "deprecated" in legacy_capture.err
    assert "X999" in legacy_capture.err

    monkeypatch.chdir(canonical)
    assert main(["check", "sample.py"]) == 2
    canonical_capture = capsys.readouterr()
    assert "Unknown select rule selector(s): X999" in canonical_capture.err


def test_cli_no_rule_plugins_disables_installed_provider(monkeypatch, tmp_path, capsys) -> None:
    class FakeEntryPoint:
        name = "demo"
        value = "demo_rules:register_rules"
        dist = None

        def load(self):
            def register_rules(registry):
                from flakeforge import RuleViolation

                class PluginRule:
                    code = "ZZZ001"
                    description = "provider rule"

                    def check(self, context):
                        yield RuleViolation(context.filename, 1, 0, self.code, "provider rule")

                registry.register(PluginRule(), provider="demo_rules")

            return register_rules

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            if group != "flakeforge.rules":
                raise ValueError(f"unexpected entry-point group: {group!r}")
            return self

    monkeypatch.setattr(
        "flakeforge.registry.metadata.entry_points",
        lambda: FakeEntryPoints([FakeEntryPoint()]),
    )
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", str(sample), "--select", "ZZZ"]) == 1
    assert "ZZZ001" in capsys.readouterr().out
    assert main(["check", str(sample), "--select", "ZZZ", "--no-rule-plugins"]) == 2
    assert "Unknown select rule selector(s): ZZZ" in capsys.readouterr().err


def test_cli_honors_target_config_when_run_from_outside(tmp_path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    sample = project / "sample.py"
    sample.write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    monkeypatch.chdir(outside)
    # The target project ignores X001, so linting it is clean even though
    # the invocation runs from an unrelated working directory.
    assert main(["check", "--select", "X001", str(project)]) == 0

    captured = capsys.readouterr()
    assert "X001" not in captured.out
    assert captured.out.strip() == "Checked 1 file(s); no violations found."


def test_cli_select_and_ignore_override_loaded_config(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'select = ["X999"]\nignore = ["X001"]\n',
        encoding="utf-8",
    )
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X001", "--ignore", "X002", "sample.py"]) == 1

    captured = capsys.readouterr()
    assert "X001" in captured.out
    assert "Unknown select rule selector(s): X999" not in captured.err


def test_cli_nonexistent_path_is_invalid_invocation(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist"

    assert main(["check", str(missing)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: path does not exist: {missing}" in captured.err


def test_cli_no_config_ignores_hostile_toml(tmp_path, monkeypatch, capsys) -> None:
    # Acceptance: --no-config must not honour the on-disk ignore = ["X"].
    (tmp_path / "flakeforge.toml").write_text('ignore = ["X"]\n', encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--no-config", "--select", "X001", "."]) == 1
    assert "X001" in capsys.readouterr().out


def test_cli_no_config_skips_project_rule_modules(tmp_path, monkeypatch, capsys) -> None:
    # A hostile config file's rule_modules must not load under --no-config (C7).
    (tmp_path / "flakeforge.toml").write_text('rule_modules = ["nonexistent.module"]\n', encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text('def g() -> int:\n    """Doc."""\n    return 1\n', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--no-config", "."]) == 0
    assert capsys.readouterr().out.strip() == "Checked 1 file(s); no violations found."


def test_cli_include_replaces_config_list(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('include = ["only"]\n', encoding="utf-8")
    keep = tmp_path / "pkg"
    keep.mkdir()
    (keep / "sample.py").write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    # --include replaces the file's ["only"] whitelist with pkg/*.
    assert main(["check", "--include", "pkg/*", "--select", "X001", "."]) == 1
    assert "pkg/sample.py" in capsys.readouterr().out


def test_cli_exclude_replaces_config_list(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('exclude = ["pkg"]\n', encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "sample.py").write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    # --exclude replaces the file's ["pkg"], so pkg is scanned and flagged.
    assert main(["check", "--exclude", "vendor", "--select", "X001", "."]) == 1
    assert "pkg/sample.py" in capsys.readouterr().out


def test_cli_per_file_ignores_from_config_suppresses(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'per_file_ignores = { "pkg/**" = ["X001"] }\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "sample.py").write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # The config table silences X001 under pkg/, so the run is clean.
    assert main(["check", "--select", "X001", "."]) == 0
    assert "no violations found" in capsys.readouterr().out


def test_cli_per_file_ignores_flag_replaces_config_table(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'per_file_ignores = { "pkg/**" = ["X001"] }\n',
        encoding="utf-8",
    )
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "sample.py").write_text(
        "def f():\n    try:\n        run()\n    except:\n        return 1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # The CLI flag REPLACES the file's table (C2); it targets a different glob,
    # so pkg/ is no longer covered and X001 surfaces again.
    assert main(["check", "--per-file-ignores", "vendor/**:X001", "--select", "X001", "."]) == 1
    assert "pkg/sample.py" in capsys.readouterr().out


def test_cli_per_file_ignores_malformed_exits_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--per-file-ignores", "tests/**", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "malformed --per-file-ignores" in captured.err


def test_cli_config_show_reports_per_file_ignores_origin(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text(
        'per_file_ignores = { "tests/**" = ["x002"] }\n',
        encoding="utf-8",
    )
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # File value renders as a JSON object with upper-cased codes and origin "file".
    assert main(["config", "show", "--output-format", "json", "sample.py"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["settings"]["per_file_ignores"] == {
        "origin": "file",
        "value": {"tests/**": ["X002"]},
    }
    # A CLI flag overrides and is reported as origin "cli"; text renders compactly.
    assert main(["config", "show", "--per-file-ignores", "scripts/*.py:X0", "sample.py"]) == 0
    rows = _parse_show_table(capsys.readouterr().out)
    assert rows["per_file_ignores"] == ("{scripts/*.py=[X0]}", "cli")


_BARE_EXCEPT_SOURCE = "def f():\n    try:\n        run()\n    except:\n        return 1\n"


def _write_bare_except_project(tmp_path):
    """Create a project whose ``m.py`` has one X001 bare-except violation."""
    (tmp_path / "m.py").write_text(_BARE_EXCEPT_SOURCE, encoding="utf-8")


def test_cli_write_baseline_exits_zero_even_with_violations(tmp_path, monkeypatch, capsys) -> None:
    _write_bare_except_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Violations exist, yet --write-baseline records them and exits 0 (C1).
    assert main(["check", "--select", "X001", "--write-baseline", "baseline.json", "."]) == 0
    out = capsys.readouterr().out
    assert "Wrote 1 baseline entry to" in out
    document = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert len(document["entries"]) == 1


def test_cli_baseline_suppresses_then_new_surfaces(tmp_path, monkeypatch, capsys) -> None:
    _write_bare_except_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X001", "--write-baseline", "baseline.json", "."]) == 0
    capsys.readouterr()
    # Re-running against the baseline suppresses the recorded violation -> exit 0.
    assert main(["check", "--select", "X001", "--baseline", "baseline.json", "."]) == 0
    assert "no violations found" in capsys.readouterr().out
    # Editing the flagged line re-surfaces it -> exit 1.
    (tmp_path / "m.py").write_text(
        "def f():\n    try:\n        run()\n    except:  # edited\n        return 1\n",
        encoding="utf-8",
    )
    assert main(["check", "--select", "X001", "--baseline", "baseline.json", "."]) == 1
    assert "X001" in capsys.readouterr().out


def test_cli_write_baseline_ignores_existing_baseline(tmp_path, monkeypatch, capsys) -> None:
    _write_bare_except_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X001", "--write-baseline", "first.json", "."]) == 0
    capsys.readouterr()
    # Even with --baseline suppressing the violation, --write-baseline records the
    # full current set, so the new file still holds the entry.
    assert main(["check", "--select", "X001", "--baseline", "first.json", "--write-baseline", "second.json", "."]) == 0
    document = json.loads((tmp_path / "second.json").read_text(encoding="utf-8"))
    assert len(document["entries"]) == 1


def test_cli_baseline_malformed_exits_2(tmp_path, monkeypatch, capsys) -> None:
    _write_bare_except_project(tmp_path)
    (tmp_path / "baseline.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X001", "--baseline", "baseline.json", "."]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "flakeforge:" in captured.err


def test_cli_baseline_missing_file_exits_2(tmp_path, monkeypatch, capsys) -> None:
    _write_bare_except_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X001", "--baseline", "absent.json", "."]) == 2
    assert "flakeforge:" in capsys.readouterr().err


def test_cli_baseline_config_relative_resolution_regardless_of_cwd(tmp_path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "m.py").write_text(_BARE_EXCEPT_SOURCE, encoding="utf-8")
    (project / "flakeforge.toml").write_text(
        'select = ["X001"]\nbaseline = "baseline.json"\n',
        encoding="utf-8",
    )
    # Record the baseline beside the config, then reference it config-relative.
    monkeypatch.chdir(project)
    assert main(["check", "--write-baseline", "baseline.json", "."]) == 0
    capsys.readouterr()
    # From an unrelated cwd, the config's base_dir-relative baseline still resolves.
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    monkeypatch.chdir(sibling)
    assert main(["check", "--config", str(project / "flakeforge.toml"), str(project)]) == 0
    assert "no violations found" in capsys.readouterr().out


def test_cli_absolute_config_baseline_exits_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('baseline = "/tmp/baseline.json"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--config", "flakeforge.toml", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "baseline path '/tmp/baseline.json' must be relative" in captured.err


def test_cli_config_show_reports_baseline_origin(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('baseline = "ci/base.json"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "sample.py"]) == 0
    rows = _parse_show_table(capsys.readouterr().out)
    assert rows["baseline"] == ("ci/base.json", "file")


def test_cli_absolute_config_pattern_exits_error(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('include = ["/etc"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--config", "flakeforge.toml", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "include pattern '/etc' must be relative" in captured.err


def test_cli_absolute_include_flag_is_not_a_config_error(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(
        'def documented() -> int:\n    """Doc."""\n    return 1\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # Operator-supplied absolute --include is accepted, not rejected as a config
    # file would be; it never yields the exit-2 config error.
    exit_code = main(["check", "--no-config", "--include", str(tmp_path / "src")])
    assert exit_code != 2
    assert "must be relative" not in capsys.readouterr().err


def test_cli_config_and_no_config_are_mutually_exclusive(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text("select = []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", "flakeforge.toml", "--no-config", "."])
    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


_BROAD_EXCEPT = (
    "def handler() -> int:\n"
    '    """Handle a broad exception."""\n'
    "    try:\n"
    "        risky()\n"
    "    except Exception:\n"
    "        return 1\n"
)


def test_cli_output_format_json_from_config_file(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('output_format = "json"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "sample.py"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["violations"][0]["code"] == "X002"


def test_cli_output_format_cli_overrides_config_to_text(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('output_format = "json"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "text", "sample.py"]) == 1
    out = capsys.readouterr().out
    assert out.lstrip().startswith("sample.py:")


def test_cli_output_format_cli_overrides_config_to_json(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('output_format = "text"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "json", "sample.py"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["violations"][0]["code"] == "X002"


def test_cli_invalid_output_format_in_config_is_exit_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('output_format = "xml"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unknown output format" in captured.err


def _entry_points_with_zzz(monkeypatch) -> None:
    class FakeEntryPoint:
        name = "demo"
        value = "demo_rules:register_rules"
        dist = None

        def load(self):
            def register_rules(registry):
                from flakeforge import RuleViolation

                class PluginRule:
                    code = "ZZZ001"
                    description = "provider rule"

                    def check(self, context):
                        yield RuleViolation(context.filename, 1, 0, self.code, "provider rule")

                registry.register(PluginRule(), provider="demo_rules")

            return register_rules

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            if group != "flakeforge.rules":
                raise ValueError(f"unexpected entry-point group: {group!r}")
            return self

    monkeypatch.setattr(
        "flakeforge.registry.metadata.entry_points",
        lambda: FakeEntryPoints([FakeEntryPoint()]),
    )


def test_cli_rule_plugins_false_in_config_disables_provider(monkeypatch, tmp_path, capsys) -> None:
    _entry_points_with_zzz(monkeypatch)
    (tmp_path / "flakeforge.toml").write_text("rule_plugins = false\n", encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    # The provider that owns ZZZ is not loaded, so selecting it is invalid.
    assert main(["check", str(sample), "--select", "ZZZ"]) == 2
    assert "Unknown select rule selector(s): ZZZ" in capsys.readouterr().err
    # A CLI --rule-plugins overrides the file's false in the other direction.
    assert main(["check", str(sample), "--select", "ZZZ", "--rule-plugins"]) == 1
    assert "ZZZ001" in capsys.readouterr().out


def test_cli_no_rule_plugins_overrides_config_true(monkeypatch, tmp_path, capsys) -> None:
    _entry_points_with_zzz(monkeypatch)
    (tmp_path / "flakeforge.toml").write_text("rule_plugins = true\n", encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", str(sample), "--select", "ZZZ", "--no-rule-plugins"]) == 2
    assert "Unknown select rule selector(s): ZZZ" in capsys.readouterr().err


def test_cli_unknown_config_key_exits_2_with_hint_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    # G5 reproduction: a typo'd key fails loudly instead of being silently ignored.
    (tmp_path / "flakeforge.toml").write_text('exlude = ["build"]\n', encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text("x = 1\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["check", str(sample)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "flakeforge.toml: unknown key 'exlude' (did you mean 'exclude'?)" in captured.err


def test_cli_same_directory_shadow_prints_exactly_one_warning(tmp_path, monkeypatch, capsys) -> None:
    # G6 reproduction: flakeforge.toml shadows a sibling pyproject [tool.flakeforge]
    # section; exactly one warning reaches stderr and the flakeforge.toml policy wins.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flakeforge]\nselect = ["X001"]\n',
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('select = ["X002"]\n', encoding="utf-8")
    sample = tmp_path / "sample.py"
    sample.write_text(
        'def documented() -> int:\n    """Return a number."""\n    return 1\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    assert main(["check", "sample.py"]) == 0
    captured = capsys.readouterr()
    shadow_lines = [line for line in captured.err.splitlines() if "is shadowed by flakeforge.toml" in line]
    assert shadow_lines == ["flakeforge: pyproject.toml [tool.flakeforge] is shadowed by flakeforge.toml; remove one"]


def test_cli_output_format_choices_are_derived_from_registry() -> None:
    from flakeforge import api
    from flakeforge.cli import build_parser

    parser = build_parser()
    for output_format in api.FORMATTERS:
        namespace = parser.parse_args(["check", "--output-format", output_format])
        assert namespace.output_format == output_format


def test_cli_rejects_format_absent_from_registry(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--output-format", "xml", "sample.py"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_json_output_includes_schema_version(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "json", "sample.py"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["files_checked"] == 1
    assert payload["violations"][0]["code"] == "X002"


def test_cli_github_format_keeps_exit_codes_and_emits_annotation(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # C1: violations found -> exit 1, and the GitHub annotation is on stdout.
    assert main(["check", "--select", "X002", "--output-format", "github", "sample.py"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error file=sample.py,line=5,col=")
    assert "title=X002::" in out


def test_cli_github_format_clean_run_is_exit_zero_without_stray_line(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # C1: clean -> exit 0; empty github output must not print a stray blank line.
    assert main(["check", "--select", "X002", "--output-format", "github", "clean.py"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_sarif_format_keeps_exit_codes_and_emits_document(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "sarif", "sample.py"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["tool"]["driver"]["name"] == "flakeforge"
    assert doc["runs"][0]["results"][0]["ruleId"] == "X002"


def test_cli_sarif_format_clean_run_is_exit_zero_valid_document(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "sarif", "clean.py"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["runs"][0]["results"] == []
    assert doc["runs"][0]["tool"]["driver"]["rules"]  # every registered code listed


def test_cli_statistics_flag_appends_text_summary(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # C1: violations still exit 1; --statistics never changes the exit code.
    assert main(["check", "--select", "X002", "--statistics", "sample.py"]) == 1
    out = capsys.readouterr().out
    assert "sample.py:5:" in out  # normal finding line first
    assert "\n\nX002  1  " in out  # aligned per-code summary after a blank line


def test_cli_json_statistics_flag_adds_object(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "json", "--statistics", "sample.py"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["statistics"]["X002"]["count"] == 1
    assert payload["statistics"]["X002"]["description"]


def test_cli_statistics_zero_violations_omits_summary_and_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--statistics", "clean.py"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == "Checked 1 file(s); no violations found."


def test_cli_statistics_ignored_for_github_with_warning(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "github", "--statistics", "sample.py"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("::error file=sample.py")
    assert "\n\n" not in captured.out  # no summary block injected
    assert "flakeforge: statistics is ignored for the 'github' output format" in captured.err


def test_cli_statistics_ignored_for_sarif_with_warning(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--output-format", "sarif", "--statistics", "sample.py"]) == 1
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert "statistics" not in doc  # SARIF shape untouched
    assert "flakeforge: statistics is ignored for the 'sarif' output format" in captured.err


def test_cli_statistics_flag_overrides_file_true_to_false(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text("statistics = true\n", encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # File asks for statistics; --no-statistics overrides it off (C2, both directions).
    assert main(["check", "--select", "X002", "--no-statistics", "sample.py"]) == 1
    assert "\n\n" not in capsys.readouterr().out


def test_cli_statistics_flag_overrides_file_false_to_true(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text("statistics = false\n", encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--select", "X002", "--statistics", "sample.py"]) == 1
    assert "\n\nX002  1  " in capsys.readouterr().out


def test_cli_statistics_file_value_wins_without_flag(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text("statistics = true\n", encoding="utf-8")
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # No CLI flag (default None): the file's statistics=true is honoured.
    assert main(["check", "--select", "X002", "sample.py"]) == 1
    assert "\n\nX002  1  " in capsys.readouterr().out


def test_cli_statistics_from_config_file_ignored_for_github(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text(_BROAD_EXCEPT, encoding="utf-8")
    (tmp_path / "flakeforge.toml").write_text(
        'select = ["X002"]\noutput_format = "github"\nstatistics = true\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["check", "sample.py"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("::error file=sample.py")
    assert "flakeforge: statistics is ignored for the 'github' output format" in captured.err
    assert "--statistics" not in captured.err


def _parse_show_table(out: str) -> dict[str, tuple[str, str]]:
    """Parse the ``config show`` text table into ``{setting: (value, origin)}``."""
    rows: dict[str, tuple[str, str]] = {}
    body = out.split("\n\n", 1)[1]
    for line in body.splitlines()[1:]:  # skip the header row
        parts = line.split()
        rows[parts[0]] = (" ".join(parts[1:-1]), parts[-1])
    return rows


def test_config_show_defaults_no_config(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # --no-config reports pure defaults; the on-disk ignore is never consulted.
    assert main(["config", "show", "--no-config", "sample.py"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "source:   defaults"
    rows = _parse_show_table(out)
    assert rows["ignore"] == ("[]", "default")
    assert all(origin == "default" for _, origin in rows.values())


def test_config_show_discovered_file_origins(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('ignore = ["X001"]\nexclude = ["build"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "sample.py"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("flakeforge.toml")
    rows = _parse_show_table(out)
    assert rows["ignore"] == ("[X001]", "file")
    assert rows["exclude"] == ("[build]", "file")
    assert rows["select"] == ("[]", "default")


def test_config_show_cli_override_origins(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # A CLI --select overrides; --statistics flips a bool; both report "cli".
    assert main(["config", "show", "--select", "X002", "--statistics", "sample.py"]) == 0
    rows = _parse_show_table(capsys.readouterr().out)
    assert rows["select"] == ("[X002]", "cli")
    assert rows["statistics"] == ("true", "cli")
    assert rows["ignore"] == ("[X001]", "file")


def test_config_show_explicit_config_file(tmp_path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[tool.flakeforge]\nselect = ["X002"]\n', encoding="utf-8")
    (project / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "--config", str(project / "pyproject.toml"), str(project)]) == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first.endswith("pyproject.toml [tool.flakeforge]")
    assert _parse_show_table(out)["select"] == ("[X002]", "file")


def test_config_show_json_is_stable_and_sorted(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('exclude = ["build"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "--output-format", "json", "--select", "X002", "sample.py"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    # sort_keys=True => a re-dump with the same options is byte-identical.
    assert raw.strip() == json.dumps(payload, sort_keys=True, indent=2)
    assert payload["settings"]["exclude"] == {"origin": "file", "value": ["build"]}
    assert payload["settings"]["select"] == {"origin": "cli", "value": ["X002"]}
    assert payload["source"]["file"].endswith("flakeforge.toml")
    assert payload["source"]["section"] is None


def test_config_show_legacy_and_shadow_warnings_on_stderr(tmp_path, monkeypatch, capsys) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "pyproject.toml").write_text('[tool.flake8_lint]\nignore = ["X001"]\n', encoding="utf-8")
    (legacy / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", str(legacy)]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].endswith("pyproject.toml [tool.flake8_lint]")
    assert "deprecated" in captured.err


def test_config_show_invalid_config_exits_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('exlude = ["build"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown key 'exlude' (did you mean 'exclude'?)" in captured.err


def test_config_show_invalid_selector_exits_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('select = ["X999"]\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Selector validation runs against the resolved registry, exactly as check does.
    assert main(["config", "show", "sample.py"]) == 2
    assert "Unknown select rule selector(s): X999" in capsys.readouterr().err


def test_config_show_rejects_annotation_output_format(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "--output-format", "github", "sample.py"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config show supports only text and json output, not 'github'" in captured.err


def test_config_show_reports_output_format_key_from_file(tmp_path, monkeypatch, capsys) -> None:
    # Double role: a file output_format=github is reported as data, but the report
    # itself still renders as text (its own rendering is not hijacked).
    (tmp_path / "flakeforge.toml").write_text('output_format = "github"\n', encoding="utf-8")
    (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show", "sample.py"]) == 0
    out = capsys.readouterr().out
    assert _parse_show_table(out)["output_format"] == ("github", "file")


def test_config_show_missing_path_is_exit_2(tmp_path, capsys) -> None:
    missing = tmp_path / "nope"
    assert main(["config", "show", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: path does not exist: {missing}" in captured.err


def test_config_show_anchor_from_outside_the_project(tmp_path, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    (project / "sample.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    # Run from outside: discovery still anchors on the target, so the target's
    # own config is what config show reports (C4).
    assert main(["config", "show", str(project)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].endswith("project/flakeforge.toml")
    assert lines[1] == f"anchor:   {project}"
    assert lines[2] == f"base_dir: {project}"
    assert _parse_show_table(out)["ignore"] == ("[X001]", "file")


def _parse_rules_table(out: str) -> dict[str, tuple[str, str, str]]:
    """Parse the ``rules`` text table into ``{code: (state, origin, description)}``."""
    rows: dict[str, tuple[str, str, str]] = {}
    for line in out.splitlines()[1:]:  # skip the header row
        parts = line.split()
        rows[parts[0]] = (parts[1], parts[2], " ".join(parts[3:]))
    return rows


def test_cli_rules_lists_builtins_enabled_with_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-config"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "code  state    origin   description"
    rows = _parse_rules_table(out)
    assert rows["X001"] == ("enabled", "builtin", "Do not use bare except.")
    # Every built-in defaults to enabled and carries the builtin origin.
    assert all(origin == "builtin" for _, origin, _ in rows.values())
    assert all(state == "enabled" for state, _, _ in rows.values())
    # Deterministic order by code.
    codes = [line.split()[0] for line in out.splitlines()[1:]]
    assert codes == sorted(codes)


def test_cli_rules_select_from_cli_marks_disabled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-config", "--select", "X001"]) == 0
    rows = _parse_rules_table(capsys.readouterr().out)
    assert rows["X001"][0] == "enabled"
    assert rows["X002"][0] == "disabled"


def test_cli_rules_ignore_from_config_file_marks_disabled(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["rules"]) == 0
    rows = _parse_rules_table(capsys.readouterr().out)
    assert rows["X001"][0] == "disabled"
    assert rows["X002"][0] == "enabled"


def test_cli_rules_reports_rule_module_origin(tmp_path, monkeypatch, capsys) -> None:
    package = tmp_path / "demo_project"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lint_rules.py").write_text(
        textwrap.dedent(
            """
            from flakeforge import RuleRegistry, RuleViolation

            class LocalRule:
                code = "ORG001"
                description = "Local rule"

                def check(self, context):
                    yield RuleViolation(context.filename, 1, 0, self.code, "local rule")

            def register_rules(registry: RuleRegistry) -> None:
                registry.register(LocalRule(), provider="demo_project.lint_rules")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "flakeforge.toml").write_text('rule_modules = ["demo_project.lint_rules"]\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert main(["rules"]) == 0
    rows = _parse_rules_table(capsys.readouterr().out)
    assert rows["ORG001"] == ("enabled", "rule_module:demo_project.lint_rules", "Local rule")


def test_cli_rules_reports_entry_point_origin(monkeypatch, tmp_path, capsys) -> None:
    _entry_points_with_zzz(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["rules"]) == 0
    rows = _parse_rules_table(capsys.readouterr().out)
    assert rows["ZZZ001"] == ("enabled", "entry_point:demo", "provider rule")


def test_cli_rules_no_rule_plugins_hides_entry_point_rules(monkeypatch, tmp_path, capsys) -> None:
    _entry_points_with_zzz(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-rule-plugins"]) == 0
    assert "ZZZ001" not in capsys.readouterr().out


def test_cli_rules_no_config_hides_project_rule_modules(tmp_path, monkeypatch, capsys) -> None:
    # A config's rule_modules must not load under --no-config (C7); the module
    # here does not even exist, so loading it would be an error rather than silent.
    (tmp_path / "flakeforge.toml").write_text('rule_modules = ["nonexistent.module"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-config"]) == 0
    out = capsys.readouterr().out
    assert "nonexistent" not in out
    assert "X001" in out


def test_cli_rules_json_is_stable_and_sorted(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-config", "--output-format", "json", "--select", "X001"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    # sort_keys=True => a re-dump with the same options is byte-identical.
    assert raw.strip() == json.dumps(payload, sort_keys=True, indent=2)
    codes = [rule["code"] for rule in payload["rules"]]
    assert codes == sorted(codes)
    first = payload["rules"][0]
    assert first == {
        "code": "X001",
        "description": "Do not use bare except.",
        "enabled": True,
        "origin": "builtin",
    }
    assert payload["rules"][1]["enabled"] is False


def test_cli_rules_invalid_config_exits_2(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "flakeforge.toml").write_text('select = ["X999"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Selector validation runs against the resolved registry, exactly as check does.
    assert main(["rules"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Unknown select rule selector(s): X999" in captured.err


def test_cli_rules_missing_path_is_exit_2(tmp_path, capsys) -> None:
    missing = tmp_path / "nope"
    assert main(["rules", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: path does not exist: {missing}" in captured.err


def test_cli_rules_rejects_annotation_output_format(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["rules", "--no-config", "--output-format", "sarif"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rules supports only text and json output, not 'sarif'" in captured.err


def _default_config_mapping() -> dict:
    """Return every config key mapped to the default a fresh LintConfig holds."""
    defaults = LintConfig()
    mapping = {}
    for key in CONFIG_KEYS:
        value = getattr(defaults, key)
        if key == "per_file_ignores":
            # A TOML table, not a flat array, so it round-trips as a dict.
            mapping[key] = {glob: list(codes) for glob, codes in value}
        elif isinstance(value, tuple):
            mapping[key] = list(value)
        else:
            mapping[key] = value
    return mapping


def test_init_template_keys_match_schema_and_defaults() -> None:
    # Lockstep guard: the rendered template must carry exactly CONFIG_KEYS, each
    # at its schema default, so a new schema key without a template entry fails CI.
    from flakeforge.config import render_config_template

    parsed = tomllib.loads(render_config_template(pyproject=False))
    assert tuple(parsed) == CONFIG_KEYS
    assert parsed == _default_config_mapping()
    # The pyproject surface wraps the same body under [tool.flakeforge].
    wrapped = tomllib.loads(render_config_template(pyproject=True))
    assert wrapped == {"tool": {"flakeforge": _default_config_mapping()}}


def test_init_flat_writes_defaults_and_round_trips(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    target = tmp_path / "flakeforge.toml"
    # DIR defaults to the current directory, so the printed path is relative.
    assert capsys.readouterr().out.strip() == "Wrote flakeforge.toml"
    assert target.is_file()
    # Every schema key appears at its default, one comment line per key.
    text = target.read_text(encoding="utf-8")
    for key in CONFIG_KEYS:
        assert f"\n{key} = " in f"\n{text}"
    comment_lines = [line for line in text.splitlines() if line.startswith("#")]
    assert len(comment_lines) == len(CONFIG_KEYS)
    # Round-trips through load_config to the built-in defaults, with no warnings.
    loaded = load_config(target, cwd=tmp_path)
    assert loaded == LintConfig()
    assert loaded.warnings == ()


def test_init_flat_accepts_explicit_directory(tmp_path, capsys) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert main(["init", str(project)]) == 0
    target = project / "flakeforge.toml"
    assert target.is_file()
    assert capsys.readouterr().out.strip() == f"Wrote {target}"


def test_init_pyproject_appends_and_preserves_content(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"\nversion = "0.1.0"\n'
    pyproject.write_text(original, encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == f"Wrote {pyproject}"
    text = pyproject.read_text(encoding="utf-8")
    # Existing content is preserved byte-for-byte at the head of the file.
    assert text.startswith(original)
    assert "[tool.flakeforge]" in text
    # Round-trips: the appended section loads to defaults with no warnings.
    loaded = load_config(pyproject, cwd=tmp_path)
    assert loaded == LintConfig()
    assert loaded.warnings == ()


def test_init_pyproject_adds_separator_when_no_trailing_newline(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[project]\nname = "demo"'  # no trailing newline
    pyproject.write_text(original, encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 0
    text = pyproject.read_text(encoding="utf-8")
    assert text.startswith(original)
    # A separating newline is inserted so the appended table is well-formed TOML.
    assert tomllib.loads(text)["tool"]["flakeforge"] == _default_config_mapping()


def test_init_pyproject_creates_file_when_absent(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    assert main(["init", "--pyproject", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == f"Wrote {pyproject}"
    assert pyproject.is_file()
    loaded = load_config(pyproject, cwd=tmp_path)
    assert loaded == LintConfig()
    assert loaded.warnings == ()


def test_init_refuses_existing_flakeforge_toml(tmp_path, capsys) -> None:
    target = tmp_path / "flakeforge.toml"
    target.write_text('select = ["X001"]\n', encoding="utf-8")
    assert main(["init", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: {target} already exists" in captured.err
    # The pre-existing file is left untouched.
    assert target.read_text(encoding="utf-8") == 'select = ["X001"]\n'


def test_init_pyproject_refuses_existing_canonical_section(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.flakeforge]\nselect = ["X001"]\n'
    pyproject.write_text(original, encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: {pyproject} already defines [tool.flakeforge]" in captured.err
    assert pyproject.read_text(encoding="utf-8") == original


def test_init_pyproject_refuses_existing_legacy_section(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = '[tool.flake8_lint]\nselect = ["X001"]\n'
    pyproject.write_text(original, encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: {pyproject} already defines [tool.flake8_lint]" in captured.err
    assert pyproject.read_text(encoding="utf-8") == original


def test_init_pyproject_refuses_malformed_pyproject(tmp_path, capsys) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = "this is = = not valid toml\n"
    pyproject.write_text(original, encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: {pyproject} is not valid TOML" in captured.err
    # Refusal must not append to a file it could not parse.
    assert pyproject.read_text(encoding="utf-8") == original


def test_init_nonexistent_directory_is_exit_2(tmp_path, capsys) -> None:
    missing = tmp_path / "nope"
    assert main(["init", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"flakeforge: not a directory: {missing}" in captured.err


def test_init_flat_refuses_when_pyproject_section_would_be_shadowed(tmp_path, capsys) -> None:
    # C3 shadowing: a same-dir flakeforge.toml outranks a pyproject section, so
    # writing one would silently shadow the existing [tool.flakeforge].
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.flakeforge]\nselect = ["X001"]\n', encoding="utf-8")
    assert main(["init", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "would shadow it" in captured.err
    assert not (tmp_path / "flakeforge.toml").exists()


def test_init_flat_writes_despite_malformed_sibling_pyproject(tmp_path, capsys) -> None:
    # A malformed pyproject can never be loaded, so it cannot be shadowed; the
    # flat write proceeds (mirrors config's tolerant same-dir shadow detection).
    (tmp_path / "pyproject.toml").write_text("this is = = not toml\n", encoding="utf-8")
    assert main(["init", str(tmp_path)]) == 0
    target = tmp_path / "flakeforge.toml"
    assert capsys.readouterr().out.strip() == f"Wrote {target}"
    assert load_config(target, cwd=tmp_path) == LintConfig()


def test_init_pyproject_refuses_when_flakeforge_toml_would_shadow(tmp_path, capsys) -> None:
    # The mirror case: an existing flakeforge.toml would shadow a new pyproject table.
    flat = tmp_path / "flakeforge.toml"
    flat.write_text('select = ["X001"]\n', encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    assert main(["init", "--pyproject", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "would shadow" in captured.err
    # The untouched pyproject keeps no flakeforge table.
    assert "flakeforge" not in pyproject.read_text(encoding="utf-8")


def test_cli_init_refuses_dangling_symlink_target(tmp_path, capsys) -> None:
    elsewhere = tmp_path / "elsewhere.toml"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "flakeforge.toml").symlink_to(elsewhere)

    assert main(["init", str(project)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert not elsewhere.exists()


def test_cli_init_pyproject_refuses_dangling_symlink_target(tmp_path, capsys) -> None:
    elsewhere = tmp_path / "elsewhere.toml"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").symlink_to(elsewhere)

    assert main(["init", "--pyproject", str(project)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert not elsewhere.exists()


def test_cli_bare_config_command_is_invalid_invocation(capsys) -> None:
    assert main(["config"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config requires a subcommand" in captured.err
