"""Unit tests for defensive and rarely-taken branches across the engine.

These target internals directly (private helpers included) where reaching the
branch through the public surface would need an unrealistic environment, e.g.
a tokenizer failure on source the AST already accepted, or a TOML document
whose keys are not strings. Where a public surface *can* reach the branch it
is exercised through that surface instead.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import flakeforge.plugin as plugin_module
from flakeforge import check_file, check_source, check_tree
from flakeforge.api import (
    RuleViolation,
    _extract_comment,
    _include_traversal_roots,
    _is_noqa_suppressed,
    _iter_noqa_directives,
    _noqa_codes_from_comment,
    _safe_traversal_root,
    _violation_line_text,
)
from flakeforge.baseline import BaselineError, load_baseline
from flakeforge.cli import _flakeforge_section_in, _merge_unique, main
from flakeforge.config import (
    ConfigValidationError,
    LintConfig,
    _as_config_path,
    _as_per_file_ignores,
    _as_str_tuple,
    _load_pyproject,
    load_config,
    validate_config,
)
from flakeforge.discovery import discover_python_files, path_matches_any
from flakeforge.plugin import ProjectRulesPlugin, _normalize_code_prefixes
from flakeforge.registry import (
    DuplicateRuleCodeError,
    RuleProviderLoadError,
    resolve_registry,
)
from flakeforge.rule_helpers import (
    _cached_module_imports,
    _docstring_has_structured_header,
    _expr_is_none,
    _has_proper_docstring,
    _has_test_imports,
    _resolve_module_path,
)
from flakeforge.telemetry import TelemetrySinkError, build_sink

# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_discovery_skips_nonexistent_paths(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    config = LintConfig(base_dir=tmp_path)
    assert discover_python_files([tmp_path / "missing", tmp_path], config=config) == ((tmp_path / "a.py").resolve(),)


def test_discovery_resolves_relative_paths_against_base_dir(tmp_path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    found = discover_python_files(["pkg"], config=LintConfig(base_dir=tmp_path))
    assert found == ((tmp_path / "pkg" / "m.py").resolve(),)


def test_discovery_applies_include_and_exclude_to_explicit_files(tmp_path) -> None:
    keep = tmp_path / "src" / "keep.py"
    drop = tmp_path / "other" / "drop.py"
    skip = tmp_path / "src" / "skip_me.py"
    for path in (keep, drop, skip):
        path.parent.mkdir(exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    config = LintConfig(base_dir=tmp_path, include=("src",), exclude=("src/skip_me.py",))
    assert discover_python_files([keep, drop, skip], config=config) == (keep.resolve(),)


def test_discovery_deduplicates_repeated_inputs(tmp_path) -> None:
    module = tmp_path / "a.py"
    module.write_text("x = 1\n", encoding="utf-8")
    config = LintConfig(base_dir=tmp_path)
    assert discover_python_files([module, module, tmp_path], config=config) == (module.resolve(),)


def test_discovery_ignores_non_python_explicit_files(tmp_path) -> None:
    note = tmp_path / "notes.txt"
    note.write_text("x", encoding="utf-8")
    assert discover_python_files([note], config=LintConfig(base_dir=tmp_path)) == ()


@pytest.mark.parametrize("pattern", ["", "/", "\\"])
def test_path_matches_any_ignores_empty_patterns(tmp_path, pattern: str) -> None:
    assert path_matches_any(tmp_path / "a.py", [pattern], tmp_path) is False


def test_path_matches_any_without_root_uses_absolute_posix_path(tmp_path) -> None:
    target = tmp_path / "pkg" / "a.py"
    assert path_matches_any(target, ["*/pkg/a.py"], None) is True
    assert path_matches_any(target, ["*/other/a.py"], None) is False


# ---------------------------------------------------------------------------
# api: line text, noqa parsing, traversal roots
# ---------------------------------------------------------------------------


def _violation(lineno: int, filename: str = "m.py") -> RuleViolation:
    """Build a throwaway X001 violation at *lineno* in *filename*."""
    return RuleViolation(filename=filename, lineno=lineno, col_offset=0, code="X001", message="m")


def test_violation_line_text_handles_unknown_file_and_out_of_range_lines() -> None:
    lines = {"m.py": ["first", "second"]}
    assert _violation_line_text(_violation(2), lines) == "second"
    assert _violation_line_text(_violation(1, "other.py"), lines) == ""
    assert _violation_line_text(_violation(99), lines) == ""
    assert _violation_line_text(_violation(0), lines) == ""


@pytest.mark.parametrize("lineno", [0, 3])
def test_noqa_suppression_rejects_out_of_range_lines(lineno: int) -> None:
    source = "x = 1  # noqa\ny = 2  # noqa\n"
    assert _is_noqa_suppressed(_violation(lineno), source, LintConfig(), apply_noqa=True) is False


def test_noqa_codes_tolerate_empty_chunks_and_trailing_text() -> None:
    assert _noqa_codes_from_comment("noqa: X001,, x002 explanation, ") == frozenset({"X001", "X002"})


def test_iter_noqa_directives_on_untokenisable_source_yields_nothing() -> None:
    # An unterminated triple-quoted string cannot be tokenised.
    assert list(_iter_noqa_directives("x = '''never closed  # noqa\n")) == []


def test_extract_comment_on_untokenisable_line_returns_none() -> None:
    assert _extract_comment("x = '''open  # noqa") is None
    assert _extract_comment("x = 1  # noqa: X001") == "noqa: X001"
    assert _extract_comment("x = 1") is None


def test_include_traversal_roots_skips_blank_and_missing_patterns(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    roots = _include_traversal_roots(["  ", "src/**/*.py", "src", "absent"], tmp_path)
    assert roots == (tmp_path / "src",)


def test_safe_traversal_root_for_leading_wildcard_is_the_root(tmp_path) -> None:
    assert _safe_traversal_root("*.py", tmp_path) == tmp_path
    assert _safe_traversal_root("pkg/*/m.py", tmp_path) == tmp_path / "pkg"


def test_check_tree_without_source_skips_source_dependent_rules() -> None:
    # X014 (TODO comments) and X015 (unused noqa) need the raw source.
    tree = ast.parse("x = 1  # TODO: untracked  # noqa\n")
    assert check_tree(tree, "m.py", config=LintConfig(select=("X014", "X015"))) == ()


# ---------------------------------------------------------------------------
# per-file-ignores in legacy mode (entry left with no valid codes)
# ---------------------------------------------------------------------------


def test_legacy_per_file_ignores_with_only_unknown_codes_suppresses_nothing(tmp_path, capsys, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.flake8_lint]\nselect = ["X001"]\nper_file_ignores = { "*.py" = ["ZZZ999"] }\n',
        encoding="utf-8",
    )
    module = tmp_path / "m.py"
    module.write_text("try:\n    pass\nexcept:\n    raise\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", str(module)]) == 1
    captured = capsys.readouterr()
    assert "X001" in captured.out
    assert "ZZZ999" in captured.err


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "body", "message"),
    [
        ("flakeforge.toml", '[tool]\nflakeforge = "nope"\n', "[tool.flakeforge] must be a table"),
        ("pyproject.toml", "tool = 1\n", "invalid [tool] data"),
        ("pyproject.toml", '[tool]\nflakeforge = "nope"\n', "invalid [tool.flakeforge] data"),
        ("pyproject.toml", '[tool]\nflake8_lint = "nope"\n', "invalid [tool.flake8_lint] data"),
    ],
)
def test_malformed_tool_sections_are_rejected(tmp_path, filename: str, body: str, message: str) -> None:
    config_path = tmp_path / filename
    config_path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigValidationError, match=message.replace("[", r"\[").replace("]", r"\]")):
        load_config(config_path=config_path)


def test_load_pyproject_reads_file_and_returns_none_without_tool(tmp_path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert _load_pyproject(pyproject) is None
    pyproject.write_text('[tool.flakeforge]\nselect = ["X001"]\n', encoding="utf-8")
    loaded = _load_pyproject(pyproject)
    assert loaded is not None and loaded.select == ("X001",)


def test_validate_config_deduplicates_case_variant_selectors() -> None:
    config = validate_config(LintConfig(select=("X001", "x001", "X00")), ("X001", "X002"))
    assert config.select == ("X001", "X00")


def test_coercion_helpers_accept_none_as_empty() -> None:
    assert _as_str_tuple(None, field_name="select") == ()
    assert _as_per_file_ignores(None, field_name="per_file_ignores") == ()
    assert _as_config_path(None, field_name="baseline") == ""


def test_per_file_ignores_rejects_non_string_globs() -> None:
    with pytest.raises(ConfigValidationError, match="globs must be strings"):
        _as_per_file_ignores({1: ["X001"]}, field_name="per_file_ignores")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def test_cli_without_subcommand_prints_help_and_exits_2(capsys) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().out


def test_flakeforge_section_in_returns_none_for_foreign_tool_tables() -> None:
    assert _flakeforge_section_in({"tool": {"ruff": {}}}) is None
    assert _flakeforge_section_in({"tool": "x"}) is None
    assert _flakeforge_section_in({"tool": {"flake8_lint": {}}}) == "[tool.flake8_lint]"


def test_merge_unique_preserves_order_and_drops_duplicates() -> None:
    assert _merge_unique(("a", "b"), ("b", "c", "a", "d")) == ("a", "b", "c", "d")


def test_config_show_attributes_every_cli_flag_to_cli(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    argv = [
        "config",
        "show",
        "--no-config",
        "--select",
        "X001",
        "--ignore",
        "X002",
        "--include",
        "src",
        "--exclude",
        "build",
        "--per-file-ignores",
        "a.py:X001",
        "--noqa",
        "--no-rule-plugins",
        "--statistics",
        "--baseline",
        "b.json",
        "--output-format",
        "json",
    ]
    assert main(argv) == 0
    settings = json.loads(capsys.readouterr().out)["settings"]
    cli_keys = {key for key, entry in settings.items() if entry["origin"] == "cli"}
    assert cli_keys == {
        "select",
        "ignore",
        "include",
        "exclude",
        "per_file_ignores",
        "allow_noqa",
        "rule_plugins",
        "statistics",
        "baseline",
        "output_format",
    }
    assert settings["rule_modules"]["origin"] == "default"


# ---------------------------------------------------------------------------
# plugin (Flake8 adapter)
# ---------------------------------------------------------------------------


def _flake8_options(**overrides) -> SimpleNamespace:
    """Return a Flake8-like options namespace with neutral defaults."""
    values = {
        "select": (),
        "extend_select": (),
        "ignore": (),
        "extend_ignore": (),
        "disable_noqa": False,
        "flakeforge_no_noqa": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_plugin_registers_its_option() -> None:
    recorded: list[tuple] = []
    manager = SimpleNamespace(add_option=lambda *args, **kwargs: recorded.append((args, kwargs)))
    ProjectRulesPlugin.add_options(manager)
    assert recorded and recorded[0][0][0] == "--flakeforge-no-noqa"


@pytest.mark.parametrize("filename", ["stdin", "-", None])
def test_plugin_does_not_read_pseudo_filenames(monkeypatch, filename) -> None:
    def fail_read(*_args, **_kwargs):
        raise AssertionError("pseudo filenames must not be read from disk")

    monkeypatch.setattr(plugin_module.Path, "read_text", fail_read)
    ProjectRulesPlugin.parse_options(_flake8_options(select=("X001",)))
    tree = ast.parse("try:\n    pass\nexcept:\n    raise\n")
    results = list(ProjectRulesPlugin(tree, filename).run())
    assert [row[0] for row in results] == [3]


def test_plugin_tolerates_unreadable_files(tmp_path) -> None:
    ProjectRulesPlugin.parse_options(_flake8_options(select=("X001",)))
    tree = ast.parse("try:\n    pass\nexcept:\n    raise\n")
    results = list(ProjectRulesPlugin(tree, str(tmp_path / "vanished.py")).run())
    assert [row[2].split()[0] for row in results] == ["X001"]


def test_normalize_code_prefixes_merges_and_deduplicates() -> None:
    assert _normalize_code_prefixes(("x001", "X002"), None, ["X001", "x003"]) == ("X001", "X002", "X003")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def _install_fake_entry_points(monkeypatch, register_rules) -> None:
    """Patch entry-point discovery to yield one provider calling *register_rules*."""

    class FakeEntryPoint:
        name = "dup"
        value = "dup_rules:register_rules"
        dist = None

        def load(self):
            return register_rules

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            return self

    monkeypatch.setattr("flakeforge.registry.metadata.entry_points", lambda: FakeEntryPoints([FakeEntryPoint()]))


def test_entry_point_colliding_with_builtin_code_propagates_duplicate_error(monkeypatch) -> None:
    def register_rules(registry):
        class Clash:
            code = "X001"
            description = "collides with a built-in"

            def check(self, context):
                return iter(())

        registry.register(Clash(), provider="dup_rules")

    _install_fake_entry_points(monkeypatch, register_rules)
    with pytest.raises(DuplicateRuleCodeError):
        resolve_registry()


def test_entry_point_raising_runtime_error_is_wrapped(monkeypatch) -> None:
    def register_rules(registry):
        raise RuntimeError("boom")

    _install_fake_entry_points(monkeypatch, register_rules)
    with pytest.raises(RuleProviderLoadError, match="boom"):
        resolve_registry()


@pytest.mark.parametrize("body", ["X = 1\n", "register_rules = 42\n"])
def test_rule_module_without_callable_register_rules_fails(tmp_path, body: str) -> None:
    module_name = f"ff_no_register_{abs(hash(body))}"
    (tmp_path / f"{module_name}.py").write_text(body, encoding="utf-8")
    with pytest.raises(RuleProviderLoadError, match="register_rules"):
        resolve_registry(rule_modules=[module_name], include_entry_points=False, project_root=tmp_path)


# ---------------------------------------------------------------------------
# baseline / telemetry
# ---------------------------------------------------------------------------


def test_baseline_with_invalid_utf8_raises_baseline_error(tmp_path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(BaselineError, match="cannot read baseline file"):
        load_baseline(baseline)


def test_sqlite_sink_on_a_directory_path_raises_sink_error(tmp_path) -> None:
    with pytest.raises(TelemetrySinkError, match="Cannot open telemetry sink"):
        build_sink(f"sqlite:{tmp_path}")


def test_custom_sink_with_non_callable_attribute_raises_sink_error(tmp_path) -> None:
    (tmp_path / "ff_sink_attr.py").write_text("factory = 'not callable'\n", encoding="utf-8")
    with pytest.raises(TelemetrySinkError, match="does not define a callable factory"):
        build_sink("ff_sink_attr:factory", project_root=tmp_path)


# ---------------------------------------------------------------------------
# rule helpers (X003 import graph, X005 docstrings)
# ---------------------------------------------------------------------------


def _package(root: Path, name: str, modules: dict[str, str]) -> Path:
    """Create package *name* under *root* holding the given module sources."""
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for module, source in modules.items():
        (package / f"{module}.py").write_text(source, encoding="utf-8")
    return package


X003 = LintConfig(select=("X003",))


@pytest.mark.parametrize(
    ("label", "beta_source", "expect_cycle"),
    [
        ("async-deferred", "async def f():\n    import pkg.alpha\n", False),
        ("method-deferred", "class K:\n    def m(self):\n        import pkg.alpha\n", False),
        ("type-checking", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import pkg.alpha\n", False),
        ("inside-try", "try:\n    import pkg.alpha\nexcept ValueError:\n    raise\n", True),
        ("relative", "from . import alpha\n", True),
        ("from-module", "from pkg.alpha import NAME\n", True),
    ],
)
def test_x003_import_timing_edge_cases(tmp_path, label: str, beta_source: str, expect_cycle: bool) -> None:
    package = _package(tmp_path, "pkg", {"alpha": "import pkg.beta\nNAME = 1\n", "beta": beta_source})
    codes = [v.code for v in check_file(package / "alpha.py", config=X003)]
    assert codes == (["X003"] if expect_cycle else []), label


def test_x003_three_module_cycle_and_missing_targets(tmp_path) -> None:
    package = _package(
        tmp_path,
        "pkg",
        {
            "a": "import pkg.b\nimport pkg.does_not_exist\n",
            "b": "import pkg.c\n",
            "c": "import pkg.a\n",
        },
    )
    for module in ("a", "b", "c"):
        assert [v.code for v in check_file(package / f"{module}.py", config=X003)] == ["X003"]


def test_x003_relative_import_beyond_top_level_is_ignored(tmp_path) -> None:
    package = _package(tmp_path, "pkg", {"a": "from .... import nothing\nX = 1\n"})
    assert check_file(package / "a.py", config=X003) == ()


def test_resolve_module_path_and_unreadable_neighbour(tmp_path) -> None:
    assert _resolve_module_path(tmp_path, "") is None
    assert _cached_module_imports(tmp_path / "missing.py", tmp_path, "missing") == ()


def test_has_proper_docstring_edge_cases() -> None:
    assert _has_proper_docstring(ast.Module(body=[], type_ignores=[]), None) is False
    assert _has_proper_docstring(ast.parse("x = 1\n"), None) is False
    assert _has_proper_docstring(ast.parse("1\n"), None) is False
    assert _has_proper_docstring(ast.parse("'doc'\n"), None) is True
    assert _has_proper_docstring(ast.parse("'doc'\n"), ["'doc'"]) is False
    assert _has_proper_docstring(ast.parse('"""doc"""\n'), ['"""doc"""']) is True


def test_structured_header_on_blank_docstring_is_false() -> None:
    assert _docstring_has_structured_header("\n   \n") is False


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import unittest\n", True),
        ("import pytest.mark\n", True),
        ("from pytest import fixture\n", True),
        ("from unittest.mock import patch\n", True),
        ("import os\nfrom os import path\n", False),
        ("from . import sibling\n", False),
    ],
)
def test_has_test_imports(source: str, expected: bool) -> None:
    assert _has_test_imports(ast.parse(source)) is expected


def test_expr_is_none_accepts_legacy_name_node() -> None:
    assert _expr_is_none(ast.Name(id="None")) is True
    assert _expr_is_none(ast.Constant(value=None)) is True
    assert _expr_is_none(ast.Name(id="nothing")) is False
    assert _expr_is_none(None) is False


def test_x005_treats_async_test_methods_like_sync_ones() -> None:
    template = "import pytest\n\n\nclass TestThing:\n    {kw}def test_it(self):\n        assert True\n"
    config = LintConfig(select=("X005",))
    sync = check_source(template.format(kw=""), filename="test_thing.py", config=config)
    async_ = check_source(template.format(kw="async "), filename="test_thing.py", config=config)
    assert sync, "fixture must trip X005 for the parity check to mean anything"
    assert [(v.lineno, v.message) for v in async_] == [(v.lineno, v.message) for v in sync]
