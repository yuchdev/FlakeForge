import ast
import os
from pathlib import Path

from flakeforge import RuleContext, RuleRegistry, RuleViolation, check_source, check_tree
from flakeforge.config import LintConfig
from flakeforge.registry import resolve_registry

SOURCE = "\n".join(
    [
        "def handler() -> int:",
        '    """Handle a broad exception."""',
        "    try:",
        "        risky()",
        "    except Exception:  # noqa: X002",
        "        return 1",
    ]
)


class InlineRule:
    code = "ORG001"
    description = "inline"

    def check(self, context: RuleContext):
        yield RuleViolation(context.filename, 1, 0, self.code, "inline violation")


def test_bare_noqa_suppresses_matching_line() -> None:
    source = SOURCE.replace("# noqa: X002", "# noqa")
    violations = check_tree(
        ast.parse(source, filename="sample.py"),
        "sample.py",
        source,
        config=LintConfig(select=("X002",)),
    )
    assert violations == ()


def test_matching_code_noqa_suppresses_only_matching_rule() -> None:
    violations = check_tree(
        ast.parse(SOURCE, filename="sample.py"),
        "sample.py",
        SOURCE,
        config=LintConfig(select=("X002",)),
    )
    assert violations == ()


def test_multiple_codes_noqa_suppresses_matching_rule() -> None:
    source = SOURCE.replace("# noqa: X002", "# noqa: X001, X002")
    violations = check_tree(
        ast.parse(source, filename="sample.py"),
        "sample.py",
        source,
        config=LintConfig(select=("X002",)),
    )
    assert violations == ()


def test_nonmatching_noqa_does_not_suppress() -> None:
    source = SOURCE.replace("# noqa: X002", "# noqa: X001")
    violations = check_tree(
        ast.parse(source, filename="sample.py"),
        "sample.py",
        source,
        config=LintConfig(select=("X002",)),
    )
    assert [violation.code for violation in violations] == ["X002"]


def test_allow_noqa_false_disables_noqa_suppression() -> None:
    violations = check_tree(
        ast.parse(SOURCE, filename="sample.py"),
        "sample.py",
        SOURCE,
        config=LintConfig(select=("X002",), allow_noqa=False),
    )
    assert [violation.code for violation in violations] == ["X002"]


def test_source_absent_disables_noqa_suppression() -> None:
    violations = check_tree(
        ast.parse(SOURCE, filename="sample.py"),
        "sample.py",
        None,
        config=LintConfig(select=("X002",)),
    )
    assert [violation.code for violation in violations] == ["X002"]


def test_noqa_allowed_path_allows_only_matching_files(tmp_path) -> None:
    filename = str(tmp_path / "src" / "sample.py")
    violations = check_tree(
        ast.parse(SOURCE, filename=filename),
        filename,
        SOURCE,
        config=LintConfig(
            select=("X002",),
            noqa_allowed=("src/sample.py",),
            base_dir=tmp_path,
        ),
    )
    assert violations == ()


def test_noqa_forbidden_path_wins_over_noqa_allowed(tmp_path) -> None:
    filename = str(tmp_path / "src" / "sample.py")
    violations = check_tree(
        ast.parse(SOURCE, filename=filename),
        filename,
        SOURCE,
        config=LintConfig(
            select=("X002",),
            noqa_allowed=("src",),
            noqa_forbidden=("src/sample.py",),
            base_dir=tmp_path,
        ),
    )
    assert [violation.code for violation in violations] == ["X002"]


def test_noqa_path_policy_matches_base_relative_regardless_of_cwd(tmp_path) -> None:
    filename = str(tmp_path / "src" / "sample.py")
    outcomes = []
    cwd = Path.cwd()
    for run_dir in (tmp_path, tmp_path.parent):
        try:
            os.chdir(run_dir)
            violations = check_tree(
                ast.parse(SOURCE, filename=filename),
                filename,
                SOURCE,
                config=LintConfig(
                    select=("X002",),
                    noqa_forbidden=("src/sample.py",),
                    base_dir=tmp_path,
                ),
            )
        finally:
            os.chdir(cwd)
        outcomes.append(tuple(violation.code for violation in violations))

    # The noqa_forbidden policy matches the base-relative path in both runs, so
    # the X002 suppression comment is never honoured whatever the cwd.
    assert outcomes[0] == outcomes[1] == ("X002",)


def test_noqa_parsing_ignores_hash_inside_string_literals() -> None:
    registry = RuleRegistry()
    registry.register(InlineRule(), provider="tests.inline")
    source = 'value = "# not a comment"  # noqa: ORG001\n'
    tree = ast.parse(source, filename="sample.py")
    violations = check_tree(
        tree,
        "sample.py",
        source,
        config=LintConfig(select=("ORG001",), noqa_allowed=("sample.py",)),
        registry=registry,
    )
    assert violations == ()


def test_custom_rule_code_is_suppressed_by_matching_noqa() -> None:
    registry = RuleRegistry()
    registry.register(InlineRule(), provider="tests.inline")
    source = "x = 1  # noqa: ORG001\n"
    tree = ast.parse(source, filename="sample.py")
    violations = check_tree(
        tree,
        "sample.py",
        source,
        config=LintConfig(select=("ORG",), noqa_allowed=("sample.py",)),
        registry=registry,
    )
    assert violations == ()


# --------------------------------------------------------------------------- #
# X015 unused-noqa detection (engine-emitted, 05.0/03)                     #
# --------------------------------------------------------------------------- #

_REG = resolve_registry(include_entry_points=False)


def _check_x015(source: str, filename: str = "sample.py", **config_kwargs):
    """Run the shared engine over *source* with the full built-in registry."""
    return check_tree(
        ast.parse(source, filename=filename),
        filename,
        source,
        config=LintConfig(**config_kwargs),
        registry=_REG,
    )


def test_x015_reports_bare_unused_noqa() -> None:
    violations = _check_x015("x = 1  # noqa\n", select=("X015",))
    # Position is the line of the comment and the column of the `#`.
    assert [(v.code, v.lineno, v.col_offset) for v in violations] == [("X015", 1, 7)]


def test_x015_reports_coded_unused_noqa_for_enabled_code() -> None:
    violations = _check_x015("x = 1  # noqa: X001\n", select=("X001", "X015"))
    assert [v.code for v in violations] == ["X015"]


def test_x015_reports_unknown_code_noqa() -> None:
    violations = _check_x015("x = 1  # noqa: ZZZ999\n", select=("X015",))
    assert [v.code for v in violations] == ["X015"]


def test_x015_not_reported_for_used_coded_noqa() -> None:
    assert _check_x015(SOURCE, select=("X002", "X015")) == ()


def test_x015_not_reported_for_used_bare_noqa() -> None:
    source = SOURCE.replace("# noqa: X002", "# noqa")
    assert _check_x015(source, select=("X002", "X015")) == ()


def test_x015_not_reported_for_disabled_code_noqa() -> None:
    # X002 is named but not enabled this run (the subset caveat): another run,
    # e.g. CI's `--select` subset, may still honour it, so it is not X015.
    assert _check_x015(SOURCE, select=("X015",)) == ()


def test_x015_not_reported_when_per_file_ignores_cover_the_line() -> None:
    # X002 is dropped by per_file_ignores *before* the noqa check, so the noqa
    # never suppresses it directly. The per-file-ignored violation still counts
    # as covering the noqa directive (X002) on its line (the seam), so no
    # false X015 is raised.
    violations = check_tree(
        ast.parse(SOURCE, filename="sample.py"),
        "sample.py",
        SOURCE,
        config=LintConfig(
            select=("X002", "X015"),
            per_file_ignores=(("sample.py", ("X002",)),),
            base_dir=Path.cwd(),
        ),
        registry=_REG,
    )
    assert violations == ()


def test_x015_suppressed_when_allow_noqa_false() -> None:
    # With allow_noqa disabled every noqa directive is inert, so none is "unused".
    assert _check_x015("x = 1  # noqa\n", select=("X015",), allow_noqa=False) == ()


def test_x015_suppressed_by_noqa_forbidden_path_policy(tmp_path) -> None:
    filename = str(tmp_path / "sample.py")
    source = "x = 1  # noqa\n"
    violations = check_tree(
        ast.parse(source, filename=filename),
        filename,
        source,
        config=LintConfig(select=("X015",), noqa_forbidden=("sample.py",), base_dir=tmp_path),
        registry=_REG,
    )
    assert violations == ()


def test_x015_is_not_self_suppressible() -> None:
    # A noqa directive naming X015 cannot silence the very X015 it would produce.
    violations = _check_x015("x = 1  # noqa: X015\n", select=("X015",))
    assert [v.code for v in violations] == ["X015"]


def test_x015_respects_ignore() -> None:
    assert _check_x015("x = 1  # noqa\n", ignore=("X015",)) == ()


def test_x015_not_emitted_when_not_selected() -> None:
    assert _check_x015("x = 1  # noqa\n", select=("X001",)) == ()


def test_x015_enabled_by_default() -> None:
    assert [v.code for v in _check_x015("x = 1  # noqa\n")] == ["X015"]


def test_x015_can_be_dropped_by_per_file_ignores() -> None:
    # An X015 finding is itself subject to per_file_ignores like any other code.
    violations = check_tree(
        ast.parse("x = 1  # noqa\n", filename="sample.py"),
        "sample.py",
        "x = 1  # noqa\n",
        config=LintConfig(
            select=("X015",),
            per_file_ignores=(("sample.py", ("X015",)),),
            base_dir=Path.cwd(),
        ),
        registry=_REG,
    )
    assert violations == ()


def test_unused_noqa_not_emitted_when_registry_lacks_x015() -> None:
    class NoPrintRule:
        code = "ACME001"
        description = "Do not call print()."

        def check(self, context):
            return ()

    violations = check_source("value = 1  # noqa\n", rules=[NoPrintRule()])

    assert violations == ()
