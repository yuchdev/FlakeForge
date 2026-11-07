from pathlib import Path

import pytest

from flakeforge import check_file, check_source
from flakeforge.config import LintConfig
from flakeforge.registry import resolve_registry

SAMPLES = Path(__file__).parent / "samples"

# (code, filename) pairs: each violation sample must report its target code at
# least once when only that code is selected.
VIOLATION_SAMPLES = [
    ("X001", "x001_bare_except_violation.py"),
    ("X002", "x002_broad_exception_violation.py"),
    ("X003", "x003_circular_import_pkg/alpha.py"),
    ("X004", "x004_muted_exception_violation.py"),
    ("X004", "x004_muted_exception_violation_comment.py"),
    ("X005", "x005_missing_docstring_violation.py"),
    ("X005", "x005_structured_test_docstring_violation.py"),
    ("X006", "x006_local_import_violation.py"),
    ("X007", "x007_missing_return_annotation_violation.py"),
    ("X008", "x008_none_return_annotation_violation.py"),
    ("X009", "x009_percent_formatting_violation.py"),
    ("X010", "x010_import_error_suppression_violation.py"),
    ("X011", "x011_optional_violation.py"),
    ("X012", "x012_union_type_violation.py"),
    ("X013", "x013_non_raii_resource_violation.py"),
    ("X014", "x014_todo_annotation_violation.py"),
    ("X015", "x015_unused_noqa_violation.py"),
]

# Clean/near-miss samples that are clean under the full default rule set.
FULLY_CLEAN_SAMPLES = [
    "x001_bare_except_clean.py",
    "x002_broad_exception_clean.py",
    "x003_circular_import_clean.py",
    "x004_muted_exception_clean.py",
    "x005_single_quoted_docstring_clean.py",
    "x005_complete_structured_test_docstring_clean.py",
    "x006_local_import_clean.py",
    "x007_missing_return_annotation_clean.py",
    "x009_percent_formatting_clean.py",
    "x010_import_error_suppression_clean.py",
    "x011_optional_clean.py",
    "x012_union_type_clean.py",
    "x013_non_raii_resource_clean.py",
    "x014_todo_annotation_clean.py",
    "x015_unused_noqa_clean.py",
]


@pytest.mark.parametrize(("code", "filename"), VIOLATION_SAMPLES)
def test_violation_sample_reports_its_code(code: str, filename: str) -> None:
    violations = check_file(SAMPLES / filename, config=LintConfig(select=(code,)))
    assert code in [violation.code for violation in violations]


@pytest.mark.parametrize("filename", FULLY_CLEAN_SAMPLES)
def test_clean_sample_is_clean_under_all_builtin_rules(filename: str) -> None:
    assert check_file(SAMPLES / filename) == ()


def test_x008_stub_sample_is_clean_for_its_target_rule() -> None:
    # A bare `-> None: ...` stub is X008-exempt, but a stub cannot carry a
    # docstring without breaking the stub-body exemption, so it still trips
    # X005; assert cleanliness for its target rule only.
    sample = SAMPLES / "x008_none_return_annotation_clean.py"
    assert check_file(sample, config=LintConfig(select=("X008",))) == ()


@pytest.mark.parametrize(
    ("code", "source"),
    [
        ("X001", "def f():\n    try:\n        run()\n    except:\n        return 1\n"),
        (
            "X002",
            "def f():\n"
            '    """Handle a broad exception."""\n'
            "    try:\n"
            "        run()\n"
            "    except Exception:\n"
            "        raise\n",
        ),
        ("X004", "def f():\n    try:\n        run()\n    except RuntimeError:\n        pass\n"),
        ("X005", "def f():\n    return 1\n"),
        ("X006", "def f():\n    import math\n    return math.ceil(1.2)\n"),
        ("X007", 'def f():\n    """Return a value."""\n    return 1\n'),
        ("X008", "def f() -> None:\n    pass\n"),
        ("X009", 'value = "%s" % name\n'),
        (
            "X010",
            "try:\n    import missing\nexcept ImportError:\n    fallback = True\n",
        ),
        ("X011", "value: int | None = None\n"),
        ("X012", "value: int | str = 1\n"),
        ("X013", "import subprocess\n\nproc = subprocess.Popen(['echo'])\n"),
        ("X014", "# TODO: fix this later\n"),
    ],
)
def test_each_builtin_rule_fires_on_a_direct_sample(code: str, source: str) -> None:
    violations = check_source(source, filename="sample.py", config=LintConfig(select=(code,)))
    assert [violation.code for violation in violations] == [code]


def test_registry_registers_x003_as_an_active_rule() -> None:
    registry = resolve_registry(include_entry_points=False)
    registration = registry.get("X003")
    assert registration.reserved is False
    assert registration.enabled is True


def test_clean_sample_is_clean_for_all_builtin_rules() -> None:
    sample = SAMPLES / "valid_clean_module.py"
    assert check_file(sample) == ()


def test_x007_ignores_returns_inside_nested_classes(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def outer():\n"
        '    """Outer wrapper."""\n'
        "    class Inner:\n"
        '        """Inner class."""\n'
        "        def method(self) -> int:\n"
        '            """Return a value."""\n'
        "            return 1\n",
        encoding="utf-8",
    )
    violations = check_file(sample, config=LintConfig(select=("X007",)))
    assert violations == ()


def test_x006_flags_imports_inside_class_bodies(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        'class Demo:\n    """Demo class."""\n    import math\n',
        encoding="utf-8",
    )
    violations = check_file(sample, config=LintConfig(select=("X006",)))
    assert [violation.code for violation in violations] == ["X006"]


def test_x008_skips_stub_functions(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def placeholder() -> None:\n    ...\n",
        encoding="utf-8",
    )
    violations = check_file(sample, config=LintConfig(select=("X008",)))
    assert violations == ()


def test_union_rules_only_match_top_level_pep604_annotations(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "data: list[int | None] = []\nmapping: dict[str, int | float] = {}\n",
        encoding="utf-8",
    )
    violations = check_file(sample, config=LintConfig(select=("X011", "X012")))
    assert violations == ()


def test_x012_direct_samples_cover_violation_and_clean_case() -> None:
    violating = check_file(SAMPLES / "x012_union_type_violation.py", config=LintConfig(select=("X012",)))
    clean = check_file(SAMPLES / "x012_union_type_clean.py", config=LintConfig(select=("X012",)))
    assert [violation.code for violation in violating] == ["X012"]
    assert clean == ()


def test_x011_and_x012_do_not_double_report_same_annotation() -> None:
    violations = check_source(
        "value: int | None = None\n",
        filename="sample.py",
        config=LintConfig(select=("X011", "X012")),
    )
    assert [violation.code for violation in violations] == ["X011"]


def test_docstring_rule_accepts_single_quoted_docstrings(tmp_path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def documented():\n    '''Single-quoted docstring.'''\n    return 1\n",
        encoding="utf-8",
    )
    violations = check_file(sample, config=LintConfig(select=("X005",)))
    assert violations == ()


def test_x005_reports_missing_structured_test_docstring_for_test_functions() -> None:
    source = "def test_case():\n    return 1\n"
    violations = check_source(
        source,
        filename="test_sample.py",
        config=LintConfig(select=("X005",)),
    )
    assert [violation.code for violation in violations] == ["X005"]


def test_x005_reports_incomplete_structured_test_docstring() -> None:
    source = 'def test_case():\n    """[Unit] demo\\n\\nScenario: test\\n"""\n    return 1\n'
    violations = check_source(
        source,
        filename="tests/test_sample.py",
        config=LintConfig(select=("X005",)),
    )
    assert [violation.code for violation in violations] == ["X005"]


def test_x005_accepts_structured_test_docstring() -> None:
    source = (
        "def test_case():\n"
        '    """[Unit] demo\\n\\nScenario: test\\nBoundaries: none\\n'
        '    On failure, first check: inputs\\n"""\n'
        "    return None\n"
    )
    violations = check_source(
        source,
        filename="tests/test_sample.py",
        config=LintConfig(select=("X005",)),
    )
    assert violations == ()


def test_x013_reports_resource_calls_in_return_expressions() -> None:
    violations = check_source(
        "import subprocess\n\n"
        "def spawn():\n"
        '    """Spawn a subprocess."""\n'
        "    return subprocess.Popen(['echo'])\n",
        filename="sample.py",
        config=LintConfig(select=("X013",)),
    )
    assert [violation.code for violation in violations] == ["X013"]


def test_x013_still_applies_to_non_test_modules_that_import_pytest() -> None:
    violations = check_source(
        "import pytest\nimport subprocess\n\nproc = subprocess.Popen(['echo'])\n",
        filename="helper.py",
        config=LintConfig(select=("X013",)),
    )
    assert [violation.code for violation in violations] == ["X013"]


def test_x013_skips_calls_inside_with_context_expressions() -> None:
    violations = check_source(
        "import contextlib\n"
        "import socket\n\n"
        "def open_socket() -> None:\n"
        '    """Open and close a socket."""\n'
        "    with contextlib.closing(socket.socket()) as sock:\n"
        "        sock.close()\n",
        filename="sample.py",
        config=LintConfig(select=("X013",)),
    )
    assert violations == ()
