"""Coverage for X003 (circular imports) and its import-graph helpers."""

from __future__ import annotations

import ast
from pathlib import Path

from flakeforge import check_file, check_source
from flakeforge.config import LintConfig
from flakeforge.rule_helpers import (
    ModuleImport,
    ModuleImportGraph,
    _cached_module_imports,
    build_import_graph,
    extract_module_imports,
    module_location,
)

SAMPLES = Path(__file__).parent / "samples"
ONLY_X003 = LintConfig(select=("X003",))


def _make_package(root: Path, package: str, modules: dict[str, str]) -> Path:
    """Create *package* under *root*, one module file per entry in *modules*.

    An ``__init__`` entry overrides the default empty package initialiser, which
    makes it possible to build cycles that run through the package itself.
    """
    package_dir = root / package
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('"""Fixture package."""\n', encoding="utf-8")
    for name, source in modules.items():
        (package_dir / f"{name}.py").write_text(source, encoding="utf-8")
    return package_dir


def _codes(path: Path) -> list[str]:
    """Return the codes X003 reports for the module at *path*."""
    return [violation.code for violation in check_file(path, config=ONLY_X003)]


def test_direct_two_module_cycle_is_reported_in_both_modules(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "twocycle",
        {
            "alpha": "from twocycle.beta import BETA\n\nALPHA = BETA\n",
            "beta": "from twocycle.alpha import ALPHA\n\nBETA = ALPHA\n",
        },
    )
    assert _codes(package / "alpha.py") == ["X003"]
    assert _codes(package / "beta.py") == ["X003"]


def test_indirect_cycle_across_three_modules_is_reported(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "chain",
        {
            "a": "from chain.b import B\n",
            "b": "from chain.c import C\n",
            "c": "from chain.a import A\n",
        },
    )
    violations = check_file(package / "a.py", config=ONLY_X003)
    assert [violation.code for violation in violations] == ["X003"]
    assert "chain.a -> chain.b -> chain.c -> chain.a" in violations[0].message


def test_module_importing_itself_is_reported(tmp_path) -> None:
    package = _make_package(tmp_path, "selfref", {"solo": "import selfref.solo\n"})
    violations = check_file(package / "solo.py", config=ONLY_X003)
    assert [violation.code for violation in violations] == ["X003"]
    assert "selfref.solo -> selfref.solo" in violations[0].message


def test_linear_import_chain_is_clean(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "linear",
        {
            "a": "from linear.b import B\n",
            "b": "from linear.c import C\n",
            "c": "VALUE = 1\n",
        },
    )
    assert _codes(package / "a.py") == []
    assert _codes(package / "b.py") == []
    assert _codes(package / "c.py") == []


def test_relative_import_cycle_is_reported(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "relcycle",
        {
            "alpha": "from .beta import BETA\n",
            "beta": "from . import alpha\n",
        },
    )
    assert _codes(package / "alpha.py") == ["X003"]
    assert _codes(package / "beta.py") == ["X003"]


def test_cycle_through_the_package_initialiser_is_reported(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "initcycle",
        {
            "__init__": "from .alpha import ALPHA\n\nSHARED = 1\n",
            "alpha": "from initcycle import SHARED\n\nALPHA = SHARED\n",
        },
    )
    violations = check_file(package / "alpha.py", config=ONLY_X003)
    assert [violation.code for violation in violations] == ["X003"]
    assert "initcycle.alpha -> initcycle -> initcycle.alpha" in violations[0].message
    assert _codes(package / "__init__.py") == ["X003"]


def test_dotted_import_with_alias_is_reported(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "aliascycle",
        {
            "alpha": "import aliascycle.beta as beta_module\n",
            "beta": "import aliascycle.alpha\n",
        },
    )
    assert _codes(package / "alpha.py") == ["X003"]


def test_dotted_import_counts_the_package_initialiser_before_the_submodule(tmp_path) -> None:
    _make_package(
        tmp_path,
        "target",
        {
            "__init__": "from consumer.alpha import ALPHA\n",
            "beta": "BETA = 1\n",
        },
    )
    package = _make_package(tmp_path, "consumer", {"alpha": "import target.beta\n\nALPHA = 1\n"})
    violations = check_file(package / "alpha.py", config=ONLY_X003)
    assert [violation.code for violation in violations] == ["X003"]
    assert "consumer.alpha -> target -> consumer.alpha" in violations[0].message


def test_from_import_counts_the_package_initialiser_before_the_submodule(tmp_path) -> None:
    _make_package(
        tmp_path,
        "target",
        {
            "__init__": "from consumer.alpha import ALPHA\n",
            "beta": "BETA = 1\n",
        },
    )
    package = _make_package(tmp_path, "consumer", {"alpha": "from target import beta\n\nALPHA = beta.BETA\n"})
    violations = check_file(package / "alpha.py", config=ONLY_X003)
    assert [violation.code for violation in violations] == ["X003"]
    assert "consumer.alpha -> target -> consumer.alpha" in violations[0].message


def test_type_checking_import_does_not_form_a_cycle(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "typeguard",
        {
            "alpha": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from .beta import Beta\n",
            "beta": "from .alpha import ALPHA\n",
        },
    )
    assert _codes(package / "alpha.py") == []
    assert _codes(package / "beta.py") == []


def test_qualified_type_checking_guard_is_also_honoured(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "qualifiedguard",
        {
            "alpha": "import typing\n\nif typing.TYPE_CHECKING:\n    from .beta import Beta\n",
            "beta": "from .alpha import ALPHA\n",
        },
    )
    assert _codes(package / "alpha.py") == []


def test_non_typing_type_checking_attribute_still_counts_as_runtime(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "attrguard",
        {
            "alpha": "import attrguard.config as config\n\nif config.TYPE_CHECKING:\n    from .beta import BETA\n",
            "beta": "from .alpha import ALPHA\n",
            "config": "TYPE_CHECKING = True\n",
        },
    )
    assert _codes(package / "alpha.py") == ["X003"]


def test_else_branch_of_a_type_checking_guard_runs_at_import_time(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "guardelse",
        {
            "alpha": (
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n    Beta = object\nelse:\n    from .beta import Beta\n"
            ),
            "beta": "from .alpha import ALPHA\n",
        },
    )
    assert _codes(package / "alpha.py") == ["X003"]
    assert _codes(package / "beta.py") == ["X003"]


def test_function_scoped_import_does_not_form_a_cycle(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "deferred",
        {
            "alpha": "def load():\n    from .beta import BETA\n    return BETA\n",
            "beta": "from .alpha import load\n",
        },
    )
    assert _codes(package / "alpha.py") == []
    assert _codes(package / "beta.py") == []


def test_standard_library_imports_never_enter_the_graph(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "stdlibonly",
        {"alpha": "import json\nimport os.path\nfrom pathlib import Path\n"},
    )
    assert _codes(package / "alpha.py") == []


def test_unparsable_neighbour_is_treated_as_a_graph_leaf(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "broken",
        {
            "alpha": "from .beta import BETA\n",
            "beta": "def oops(:\n",
        },
    )
    assert _codes(package / "alpha.py") == []


def test_synthetic_filename_reports_nothing() -> None:
    assert check_source("import os\n", filename="<string>", config=ONLY_X003) == ()


def test_violation_anchors_on_the_offending_import_statement(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "anchor",
        {
            "alpha": '"""Doc."""\n\nimport json\n\nfrom .beta import BETA\n',
            "beta": "from .alpha import ALPHA\n",
        },
    )
    violation = check_file(package / "alpha.py", config=ONLY_X003)[0]
    assert (violation.lineno, violation.col_offset) == (5, 0)
    assert "anchor.alpha -> anchor.beta -> anchor.alpha" in violation.message


def test_each_offending_import_is_reported_separately(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "twoedges",
        {
            "alpha": "from .beta import BETA\nfrom .gamma import GAMMA\n",
            "beta": "from .alpha import ALPHA\n",
            "gamma": "from .alpha import ALPHA\n",
        },
    )
    violations = check_file(package / "alpha.py", config=ONLY_X003)
    assert [violation.lineno for violation in violations] == [1, 2]


def test_noqa_suppresses_the_circular_import(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "noqacycle",
        {
            "alpha": "from .beta import BETA  # noqa: X003\n",
            "beta": "from .alpha import ALPHA\n",
        },
    )
    assert _codes(package / "alpha.py") == []
    assert _codes(package / "beta.py") == ["X003"]


def test_x003_is_enabled_by_default(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "defaulton",
        {
            "alpha": '"""Alpha."""\n\nfrom .beta import BETA\n',
            "beta": '"""Beta."""\n\nfrom .alpha import ALPHA\n',
        },
    )
    codes = [violation.code for violation in check_file(package / "alpha.py")]
    assert codes == ["X003"]


def test_sample_package_reports_the_cycle_from_both_sides() -> None:
    package = SAMPLES / "x003_circular_import_pkg"
    assert _codes(package / "alpha.py") == ["X003"]
    assert _codes(package / "beta.py") == ["X003"]


def test_module_location_walks_up_nested_packages(tmp_path) -> None:
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (tmp_path / "outer" / "__init__.py").write_text("", encoding="utf-8")
    (inner / "__init__.py").write_text("", encoding="utf-8")
    leaf = inner / "leaf.py"
    leaf.write_text("", encoding="utf-8")

    location = module_location(str(leaf))

    assert location is not None
    assert location.name == "outer.inner.leaf"
    assert location.root == tmp_path.resolve()
    assert location.is_package is False
    assert location.package == "outer.inner"


def test_module_location_names_a_package_after_its_directory(tmp_path) -> None:
    package = tmp_path / "outer"
    package.mkdir()
    initialiser = package / "__init__.py"
    initialiser.write_text("", encoding="utf-8")

    location = module_location(str(initialiser))

    assert location is not None
    assert location.name == "outer"
    assert location.is_package is True
    assert location.package == "outer"


def test_module_location_rejects_missing_and_non_python_paths(tmp_path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("", encoding="utf-8")
    assert module_location(str(tmp_path / "missing.py")) is None
    assert module_location(str(notes)) is None
    assert module_location(str(tmp_path)) is None


def test_module_import_graph_finds_direct_and_transitive_paths() -> None:
    graph = ModuleImportGraph()
    graph.add("a", [ModuleImport("b", 1, 0)])
    graph.add("b", [ModuleImport("c", 1, 0)])
    graph.add("c", [])

    assert graph.modules() == ("a", "b", "c")
    assert graph.imports_of("a") == (ModuleImport("b", 1, 0),)
    assert graph.imports_of("unknown") == ()
    assert graph.find_path("a", "b") == ("a", "b")
    assert graph.find_path("a", "c") == ("a", "b", "c")
    assert graph.find_path("a", "a") == ("a",)
    assert graph.find_path("c", "a") == ()


def test_module_import_graph_returns_the_shortest_path() -> None:
    graph = ModuleImportGraph()
    graph.add("a", [ModuleImport("scenic", 1, 0), ModuleImport("direct", 2, 0)])
    graph.add("scenic", [ModuleImport("middle", 1, 0)])
    graph.add("middle", [ModuleImport("target", 1, 0)])
    graph.add("direct", [ModuleImport("target", 1, 0)])

    assert graph.find_path("a", "target") == ("a", "direct", "target")


def test_extract_module_imports_keeps_only_runtime_project_targets(tmp_path) -> None:
    source = (
        "import json\n"
        "from typing import TYPE_CHECKING\n"
        "from .beta import BETA\n"
        "if TYPE_CHECKING:\n"
        "    from .gamma import GAMMA\n"
        "def load():\n"
        "    from .delta import DELTA\n"
        "    return DELTA\n"
    )
    package = _make_package(
        tmp_path,
        "extract",
        {
            "alpha": source,
            "beta": "BETA = 1\n",
            "gamma": "GAMMA = 2\n",
            "delta": "DELTA = 3\n",
        },
    )
    location = module_location(str(package / "alpha.py"))
    assert location is not None

    imports = extract_module_imports(ast.parse(source), location)

    assert [module_import.module for module_import in imports] == ["extract.beta"]
    assert imports[0].lineno == 3


def test_build_import_graph_stops_at_the_import_root(tmp_path) -> None:
    package = _make_package(
        tmp_path,
        "bounded",
        {
            "alpha": "import json\n\nfrom .beta import BETA\n",
            "beta": "import os\n",
        },
    )
    location = module_location(str(package / "alpha.py"))
    assert location is not None

    graph = build_import_graph(ast.parse((package / "alpha.py").read_text()), location)

    assert graph.modules() == ("bounded.alpha", "bounded.beta")
    assert [edge.module for edge in graph.imports_of("bounded.alpha")] == ["bounded.beta"]
    assert graph.imports_of("bounded.beta") == ()


def test_long_cycles_are_not_hidden_by_graph_traversal_limits(tmp_path) -> None:
    module_count = 505
    modules = {
        f"m{index:03d}": (
            f"from longcycle.m{(index + 1) % module_count:03d} import VALUE\n"
            "VALUE = 1\n"
        )
        for index in range(module_count)
    }
    package = _make_package(tmp_path, "longcycle", modules)

    violations = check_file(package / "m000.py", config=ONLY_X003)

    assert [violation.code for violation in violations] == ["X003"]


def test_cached_module_imports_respects_module_identity(tmp_path) -> None:
    alpha = tmp_path / "shared_alpha.py"
    alpha.write_text("from .beta import BETA\n", encoding="utf-8")

    first_root = tmp_path / "first"
    first_beta = first_root / "pkg" / "beta.py"
    first_beta.parent.mkdir(parents=True)
    first_beta.write_text("BETA = 1\n", encoding="utf-8")

    second_root = tmp_path / "second"
    second_beta = second_root / "other" / "beta.py"
    second_beta.parent.mkdir(parents=True)
    second_beta.write_text("BETA = 2\n", encoding="utf-8")

    first = _cached_module_imports(alpha, first_root, "pkg.alpha")
    second = _cached_module_imports(alpha, second_root, "other.alpha")

    assert [module_import.module for module_import in first] == ["pkg.beta"]
    assert [module_import.module for module_import in second] == ["other.beta"]
