import ast
import importlib
import os
import runpy
from pathlib import Path

import pytest

from flakeforge import (
    RuleContext,
    RuleRegistry,
    RuleViolation,
    check_source,
    check_tree,
    lint_paths,
)
from flakeforge.api import LintResult
from flakeforge.config import ConfigValidationError, LintConfig


class DemoRule:
    code = "ORG001"
    description = "demo"

    def check(self, context: RuleContext):
        yield RuleViolation(context.filename, 1, 0, self.code, "demo violation")


def test_lint_result_ok_property() -> None:
    assert LintResult(violations=(), files_checked=1).ok is True


def test_check_tree_applies_select_to_custom_rules() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    tree = ast.parse("x = 1", filename="sample.py")
    config = LintConfig(select=("ORG",))
    violations = check_tree(tree, "sample.py", "x = 1\n", config=config, registry=registry)
    assert [violation.code for violation in violations] == ["ORG001"]


def test_check_tree_applies_ignore_to_custom_rules() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    tree = ast.parse("x = 1", filename="sample.py")
    violations = check_tree(
        tree,
        "sample.py",
        "x = 1\n",
        config=LintConfig(ignore=("ORG",)),
        registry=registry,
    )
    assert violations == ()


def test_check_tree_apply_noqa_false_ignores_matching_marker() -> None:
    source = "def f() -> int:\n    return 1\n"
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    violations = check_tree(
        ast.parse(source, filename="sample.py"),
        "sample.py",
        "# noqa: ORG001\n",
        apply_noqa=False,
        config=LintConfig(select=("ORG001",)),
        registry=registry,
    )
    assert [violation.code for violation in violations] == ["ORG001"]


def test_check_tree_without_source_does_not_apply_noqa() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    violations = check_tree(
        ast.parse("x = 1", filename="sample.py"),
        "sample.py",
        None,
        config=LintConfig(select=("ORG001",)),
        registry=registry,
    )
    assert [violation.code for violation in violations] == ["ORG001"]


def test_per_file_ignore_suppresses_matching_code_and_glob() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    config = LintConfig(
        select=("ORG001",),
        per_file_ignores=(("pkg/**", ("ORG001",)),),
        base_dir=Path.cwd(),
    )
    violations = check_tree(
        ast.parse("x = 1", filename="pkg/mod.py"),
        "pkg/mod.py",
        "x = 1\n",
        config=config,
        registry=registry,
    )
    assert violations == ()


def test_per_file_ignore_matches_by_code_prefix_and_glob() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")

    def run(per_file_ignores):
        config = LintConfig(select=("ORG001",), per_file_ignores=per_file_ignores, base_dir=Path.cwd())
        return check_tree(
            ast.parse("x = 1", filename="pkg/mod.py"),
            "pkg/mod.py",
            "x = 1\n",
            config=config,
            registry=registry,
        )

    # A code prefix (not just the full code) covers the violation.
    assert run((("pkg/**", ("ORG",)),)) == ()
    assert run((("pkg/**", ("ORG001",)),)) == ()
    # A glob that does not match the file leaves the violation untouched.
    assert [v.code for v in run((("other/**", ("ORG001",)),))] == ["ORG001"]


def test_per_file_ignore_applies_before_noqa_handling() -> None:
    registry = RuleRegistry()
    registry.register(DemoRule(), provider="tests.demo")
    # The line's # noqa names a different code, so it would NOT cover ORG001;
    # the violation vanishes only because per_file_ignores runs first.
    config = LintConfig(
        select=("ORG001",),
        per_file_ignores=(("pkg/**", ("ORG001",)),),
        base_dir=Path.cwd(),
    )
    suppressed = check_tree(
        ast.parse("x = 1", filename="pkg/mod.py"),
        "pkg/mod.py",
        "# noqa: X001\n",
        config=config,
        registry=registry,
    )
    assert suppressed == ()
    # Without the per-file entry the same non-covering noqa leaves it in place.
    kept = check_tree(
        ast.parse("x = 1", filename="pkg/mod.py"),
        "pkg/mod.py",
        "# noqa: X001\n",
        config=LintConfig(select=("ORG001",), base_dir=Path.cwd()),
        registry=registry,
    )
    assert [v.code for v in kept] == ["ORG001"]


def test_lint_paths_per_file_ignore_is_base_relative_regardless_of_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    pkg = project / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    config = LintConfig(
        select=("X007",),
        base_dir=project,
        config_path=project / "flakeforge.toml",
        per_file_ignores=(("pkg/**", ("X0",)),),
    )

    outputs = []
    cwd = Path.cwd()
    for run_dir in (tmp_path, sibling):
        try:
            os.chdir(run_dir)
            result = lint_paths(config=config)
        finally:
            os.chdir(cwd)
        outputs.append([violation.filename for violation in result.violations])

    # The glob resolves against base_dir, so pkg/m.py is silenced from any cwd.
    assert outputs[0] == outputs[1] == []


def test_check_source_accepts_rules_directly() -> None:
    violations = check_source(
        "x = 1\n",
        filename="sample.py",
        config=LintConfig(select=("ORG001",)),
        rules=[DemoRule()],
    )
    assert [violation.code for violation in violations] == ["ORG001"]


def test_check_source_rejects_unknown_selectors() -> None:
    with pytest.raises(ConfigValidationError):
        check_source("x = 1\n", filename="sample.py", config=LintConfig(select=("X999",)))


def test_lint_paths_defaults_to_src_and_tests_and_relativizes_output(tmp_path) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    ignored = tmp_path / "pkg"
    ignored.mkdir()
    (src / "bad.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tests / "clean.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (ignored / "also_bad.py").write_text("def nope():\n    return 1\n", encoding="utf-8")

    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        result = lint_paths(config=LintConfig(select=("X007",), base_dir=tmp_path))
    finally:
        os.chdir(cwd)

    assert result.files_checked == 2
    assert [violation.filename for violation in result.violations] == ["src/bad.py"]


def test_lint_paths_uses_include_roots_when_no_paths_are_passed(tmp_path) -> None:
    project = tmp_path / "project"
    config_dir = project / "config"
    src = project / "src"
    config_dir.mkdir(parents=True)
    src.mkdir()
    (src / "bad.py").write_text("def handler():\n    return 1\n", encoding="utf-8")

    cwd = Path.cwd()
    try:
        os.chdir(project)
        result = lint_paths(
            config=LintConfig(
                base_dir=config_dir,
                include=("../src",),
                select=("X007",),
            )
        )
    finally:
        os.chdir(cwd)

    assert result.files_checked == 1
    assert [violation.filename for violation in result.violations] == ["src/bad.py"]


def test_lint_paths_display_is_base_relative_regardless_of_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    pkg = project / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    config = LintConfig(
        select=("X007",),
        base_dir=project,
        config_path=project / "flakeforge.toml",
    )

    # An ancestor cwd (G7 reproduction) and an unrelated sibling cwd must both
    # yield the same base-relative display name.
    outputs = []
    cwd = Path.cwd()
    for run_dir in (tmp_path, sibling):
        try:
            os.chdir(run_dir)
            result = lint_paths(config=config)
        finally:
            os.chdir(cwd)
        outputs.append([violation.filename for violation in result.violations])

    assert outputs[0] == outputs[1] == ["pkg/m.py"]


def test_lint_paths_explicit_paths_override_include_filters(tmp_path) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external.py"
    project.mkdir()
    external.write_text("def handler():\n    return 1\n", encoding="utf-8")

    cwd = Path.cwd()
    try:
        os.chdir(project)
        result = lint_paths(
            paths=(external,),
            config=LintConfig(
                base_dir=project,
                include=("src/**/*.py",),
                select=("X007",),
            ),
        )
    finally:
        os.chdir(cwd)

    assert result.files_checked == 1
    assert [violation.filename for violation in result.violations] == [str(external)]


def test_package_version_falls_back_when_distribution_metadata_is_missing(monkeypatch) -> None:
    from importlib import metadata

    import flakeforge

    def missing_version(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr("importlib.metadata.version", missing_version)
    reloaded = importlib.reload(flakeforge)
    assert reloaded.__version__ == "1.1.0"


def test_python_m_entry_point_raises_system_exit(monkeypatch) -> None:
    monkeypatch.setattr("flakeforge.cli.main", lambda: 7)
    with pytest.raises(SystemExit, match="7"):
        runpy.run_module("flakeforge.__main__", run_name="__main__")


def test_formatters_registry_drives_known_formats_and_choices() -> None:
    from flakeforge import api

    assert api.KNOWN_OUTPUT_FORMATS == tuple(api.FORMATTERS)
    assert set(api.FORMATTERS) == {"text", "json", "github", "sarif"}


def test_validate_output_format_reads_registry() -> None:
    from flakeforge import api

    assert api.validate_output_format("json") == "json"
    with pytest.raises(ValueError, match="Unknown output format"):
        api.validate_output_format("xml")


def test_validate_output_format_accepts_dynamically_registered_formatter(monkeypatch) -> None:
    from flakeforge import api

    monkeypatch.setitem(api.FORMATTERS, "csv", lambda result: "csv")
    assert api.validate_output_format("csv") == "csv"
    assert api.format_result(LintResult(violations=(), files_checked=0), "csv") == "csv"


def test_format_json_carries_schema_version_without_changing_existing_keys() -> None:
    import json

    from flakeforge import api

    result = LintResult(
        violations=(RuleViolation("m.py", 1, 0, "X002", "broad except"),),
        files_checked=1,
    )
    payload = json.loads(api.format_json(result))
    assert payload["schema_version"] == api.JSON_SCHEMA_VERSION == 1
    assert payload["ok"] is False
    assert payload["files_checked"] == 1
    assert payload["violations"] == [
        {
            "filename": "m.py",
            "lineno": 1,
            "col_offset": 0,
            "code": "X002",
            "message": "broad except",
        }
    ]


_GOLDEN_DIR = Path(__file__).parent / "golden"


def test_format_github_emits_one_annotation_per_violation_with_1based_cols() -> None:
    from flakeforge import api

    result = LintResult(
        violations=(
            RuleViolation("pkg/beta.py", 12, 4, "X002", "Broad exception clause"),
            RuleViolation("pkg/alpha.py", 3, 0, "X001", "Bare except clause"),
        ),
        files_checked=2,
    )
    lines = api.format_github(result).splitlines()
    # C9 ordering: alpha before beta; col_offset 0/4 -> 1-based col 1/5.
    assert lines == [
        "::error file=pkg/alpha.py,line=3,col=1,title=X001::Bare except clause",
        "::error file=pkg/beta.py,line=12,col=5,title=X002::Broad exception clause",
    ]


def test_format_github_empty_result_is_empty_string() -> None:
    from flakeforge import api

    assert api.format_github(LintResult(violations=(), files_checked=0)) == ""


def test_format_github_escapes_message_and_property_values() -> None:
    from flakeforge import api

    # A message carries the data escapes (%, CR, LF); a filename/title carries
    # those plus ':' and ',' which otherwise terminate a property.
    result = LintResult(
        violations=(
            RuleViolation(
                "weird,name:1%.py",
                7,
                0,
                "X001",
                "bad 100% here\r\nsecond, line: end",
            ),
        ),
        files_checked=1,
    )
    line = api.format_github(result)
    assert line == (
        "::error file=weird%2Cname%3A1%25.py,line=7,col=1,title=X001::bad 100%25 here%0D%0Asecond, line: end"
    )
    # Property escaping is stricter than data escaping: ':'/',' survive raw in
    # the message but are percent-encoded in the file property.
    assert "%2C" in line and "%3A" in line  # file property
    assert "second, line: end" in line  # message keeps ':' and ','


def test_format_sarif_matches_golden_structure(monkeypatch) -> None:
    import flakeforge
    from flakeforge import api

    monkeypatch.setattr(flakeforge, "__version__", "9.9.9-golden")
    result = LintResult(
        violations=(
            RuleViolation("pkg/beta.py", 12, 4, "X002", "Broad exception clause"),
            RuleViolation("pkg/alpha.py", 3, 0, "X001", "Bare except clause"),
        ),
        files_checked=2,
        registered_rules=(
            ("X001", "Avoid bare except clauses."),
            ("X002", "Avoid catching broad exceptions."),
        ),
    )
    produced = api.format_sarif(result)
    golden = (_GOLDEN_DIR / "sarif_basic.json").read_text(encoding="utf-8")
    assert produced + "\n" == golden


def test_format_sarif_structure_cols_and_escaping() -> None:
    import json

    from flakeforge import api

    result = LintResult(
        violations=(RuleViolation("pkg/mod.py", 5, 2, "X001", "100% broken: yes, really"),),
        files_checked=1,
        registered_rules=(("X001", "Avoid bare except clauses."),),
    )
    doc = json.loads(api.format_sarif(result))
    assert doc["version"] == "2.1.0"
    assert doc["$schema"] == api.SARIF_SCHEMA_URI
    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "flakeforge"
    assert driver["version"]  # package version, non-empty
    assert driver["rules"] == [{"id": "X001", "shortDescription": {"text": "Avoid bare except clauses."}}]
    (single,) = doc["runs"][0]["results"]
    assert single["ruleId"] == "X001"
    assert single["level"] == "error"
    # JSON carries characters literally: no workflow-command escaping here.
    assert single["message"]["text"] == "100% broken: yes, really"
    region = single["locations"][0]["physicalLocation"]["region"]
    # 0-based col_offset 2 -> 1-based startColumn 3; lineno is already 1-based.
    assert region["startLine"] == 5
    assert region["startColumn"] == 3
    assert single["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "pkg/mod.py"


def test_format_sarif_empty_result_is_valid_document() -> None:
    import json

    from flakeforge import api

    doc = json.loads(api.format_sarif(LintResult(violations=(), files_checked=0)))
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"] == []
    assert doc["runs"][0]["tool"]["driver"]["rules"] == []


def test_lint_paths_populates_registered_rules(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "src"
    pkg.mkdir()
    (pkg / "m.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = lint_paths([pkg])
    codes = [code for code, _ in result.registered_rules]
    # Built-in X-codes are exposed so SARIF can list every known rule.
    assert "X001" in codes
    assert codes == sorted(codes)
    assert all(description for _, description in result.registered_rules)


def _stats_result() -> LintResult:
    """A multi-code result with registry descriptions for statistics tests."""
    return LintResult(
        violations=(
            RuleViolation("pkg/beta.py", 12, 4, "X002", "Broad exception clause"),
            RuleViolation("pkg/alpha.py", 3, 0, "X001", "Bare except clause"),
            RuleViolation("pkg/gamma.py", 9, 0, "X001", "Bare except clause"),
        ),
        files_checked=3,
        registered_rules=(
            ("X001", "Avoid bare except clauses."),
            ("X002", "Avoid catching broad exceptions."),
        ),
    )


def test_format_text_statistics_appends_aligned_sorted_summary() -> None:
    from flakeforge import api

    output = api.format_text(_stats_result(), statistics=True)
    body, _blank, summary = output.partition("\n\n")
    # The normal violation lines come first, unchanged.
    assert body.splitlines()[0].startswith("pkg/alpha.py:3:0: X001")
    # Summary sorted by code; count column right-justified; description from registry.
    assert summary.splitlines() == [
        "X001  2  Avoid bare except clauses.",
        "X002  1  Avoid catching broad exceptions.",
    ]


def test_format_text_without_statistics_has_no_summary() -> None:
    from flakeforge import api

    assert "\n\n" not in api.format_text(_stats_result())


def test_format_text_statistics_zero_violations_omits_summary() -> None:
    from flakeforge import api

    output = api.format_text(LintResult(violations=(), files_checked=2), statistics=True)
    assert output == "Checked 2 file(s); no violations found."


def test_format_text_statistics_missing_description_is_blank() -> None:
    from flakeforge import api

    # A hand-built result with no registered_rules: description falls back to "".
    result = LintResult(
        violations=(RuleViolation("m.py", 1, 0, "X001", "bare except"),),
        files_checked=1,
    )
    summary = api.format_text(result, statistics=True).partition("\n\n")[2]
    assert summary == "X001  1"


def test_format_json_statistics_object_is_additive() -> None:
    import json

    from flakeforge import api

    payload = json.loads(api.format_json(_stats_result(), statistics=True))
    # Existing keys and schema_version are unchanged by the additive key.
    assert payload["schema_version"] == 1
    assert payload["ok"] is False
    assert payload["files_checked"] == 3
    assert payload["statistics"] == {
        "X001": {"count": 2, "description": "Avoid bare except clauses."},
        "X002": {"count": 1, "description": "Avoid catching broad exceptions."},
    }


def test_format_json_without_statistics_has_no_statistics_key() -> None:
    import json

    from flakeforge import api

    payload = json.loads(api.format_json(_stats_result()))
    assert "statistics" not in payload


def test_format_json_statistics_zero_violations_is_empty_object() -> None:
    import json

    from flakeforge import api

    payload = json.loads(api.format_json(LintResult(violations=(), files_checked=0), statistics=True))
    assert payload["statistics"] == {}


def test_format_result_threads_statistics_only_for_text_and_json() -> None:
    from flakeforge import api

    result = _stats_result()
    assert "X001  2" in api.format_result(result, "text", statistics=True)
    assert '"statistics"' in api.format_result(result, "json", statistics=True)
    # github/sarif ignore statistics: identical output with or without the flag.
    for fmt in ("github", "sarif"):
        assert api.format_result(result, fmt, statistics=True) == api.format_result(result, fmt)
    assert api.STATISTICS_FORMATS == ("text", "json")
