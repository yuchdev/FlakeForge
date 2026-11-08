"""End-to-end config-precedence matrix (plan contracts C2, C3, C4, C5, C6).

This exercises the as-built precedence behaviour of task 02.0 through the real
CLI entry point (:func:`flakeforge.cli.main`). Every cell lays a known config
surface on disk, drives one invocation mode, and asserts three things: which
config file was selected (``config_path``), the effective ``ignore`` list, and
the process exit code.

The fixture sample triggers exactly one built-in code (``X001``, a bare
``except:``) and every run passes ``--select X001``, so only that code is in
play. Each config that "wins" sets ``ignore = ["X001"]``: when it is honoured the
run is clean (exit ``0``); when it is skipped or overridden the bare except fires
(exit ``1``). That makes the honoured-vs-ignored config directly observable in
the exit code without depending on any other rule.

Degenerate cells are given an explicit ruling rather than skipped:

* ``none`` + ``--config`` -- there is no config file to point at, so ``--config``
  names a non-existent path and the run is a tool failure (exit ``2``).
* ``both_same_dir`` + ``--config`` -- the same-directory shadow warning is a
  *discovery-only* signal, so pointing ``--config`` straight at ``flakeforge.toml``
  selects it silently (no shadow warning).
* ``parent_child`` discovery -- the nearest directory's ``pyproject.toml`` wins
  even though it does not ignore ``X001``, proving the parent ``flakeforge.toml``
  is never consulted (exit ``1``, not ``0``).

``cwd_inside`` and ``cwd_outside`` are intentionally expected to match: the
discovery anchor is the lint target, not the process cwd (C4), so running from
inside or outside the project yields the same selected config.
"""

import pytest

# ``_build_cli_config`` is the CLI's own config-selection step; calling it exposes the
# selected file and effective ``ignore``, which the CLI output does not show.
from flakeforge.cli import _build_cli_config, build_parser, main

# A file whose only ``--select X001`` violation is a bare ``except:``.
SAMPLE = "def f():\n    try:\n        run()\n    except:\n        return 1\n"

# Substring identifying the same-directory shadow warning on stderr.
_SHADOW = "is shadowed by flakeforge.toml"

# The ``ignore`` list produced by every winning config surface below.
_IGN = ("X001",)

VARIANTS = [
    "none",
    "flakeforge_toml",
    "pyproject_section",
    "legacy",
    "both_same_dir",
    "parent_child",
]
MODES = ["cwd_inside", "cwd_outside", "config_flag", "no_config"]

# Expected outcome per (variant, mode) cell. ``config`` is the selected file
# name or ``None`` (defaults only); ``warnings``/``forbid`` are stderr
# substrings that must/must-not appear; ``shadow`` pins the shadow-warning line
# count; ``error`` marks a cell that never builds a config (only exit is
# asserted).
EXPECT = {
    "none": {
        "cwd_inside": {"exit": 1, "ignore": (), "config": None},
        "cwd_outside": {"exit": 1, "ignore": (), "config": None},
        "config_flag": {"exit": 2, "error": True},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
    "flakeforge_toml": {
        "cwd_inside": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml"},
        "cwd_outside": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml"},
        "config_flag": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml"},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
    "pyproject_section": {
        "cwd_inside": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml"},
        "cwd_outside": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml"},
        "config_flag": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml"},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
    "legacy": {
        "cwd_inside": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml", "warnings": ["deprecated"]},
        "cwd_outside": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml", "warnings": ["deprecated"]},
        "config_flag": {"exit": 0, "ignore": _IGN, "config": "pyproject.toml", "warnings": ["deprecated"]},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
    "both_same_dir": {
        "cwd_inside": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml", "warnings": [_SHADOW], "shadow": 1},
        "cwd_outside": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml", "warnings": [_SHADOW], "shadow": 1},
        "config_flag": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml", "forbid": [_SHADOW], "shadow": 0},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
    "parent_child": {
        "cwd_inside": {"exit": 1, "ignore": (), "config": "pyproject.toml"},
        "cwd_outside": {"exit": 1, "ignore": (), "config": "pyproject.toml"},
        "config_flag": {"exit": 0, "ignore": _IGN, "config": "flakeforge.toml"},
        "no_config": {"exit": 1, "ignore": (), "config": None},
    },
}


def _build_files(variant: str, tmp_path):
    """Lay a variant's config surfaces and sample on disk under *tmp_path*.

    :param variant: One of :data:`VARIANTS`.
    :param tmp_path: The per-test temporary directory.
    :returns: Mapping with ``anchor`` (the lint target dir), ``sample`` (the
        ``.py`` file), ``outside`` (an unrelated cwd), and ``config_flag`` (the
        file ``--config`` should point at, possibly non-existent for ``none``).
    """
    outside = tmp_path / "outside"
    outside.mkdir()

    if variant == "parent_child":
        parent = tmp_path / "proj"
        child = parent / "pkg"
        child.mkdir(parents=True)
        (parent / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
        (child / "pyproject.toml").write_text("[tool.flakeforge]\nselect = []\n", encoding="utf-8")
        sample = child / "sample.py"
        sample.write_text(SAMPLE, encoding="utf-8")
        return {
            "anchor": child,
            "sample": sample,
            "outside": outside,
            "config_flag": parent / "flakeforge.toml",
        }

    anchor = tmp_path / "proj"
    anchor.mkdir()
    sample = anchor / "sample.py"
    sample.write_text(SAMPLE, encoding="utf-8")
    config_flag = anchor / "flakeforge.toml"

    if variant == "flakeforge_toml":
        (anchor / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    elif variant == "pyproject_section":
        (anchor / "pyproject.toml").write_text('[tool.flakeforge]\nignore = ["X001"]\n', encoding="utf-8")
        config_flag = anchor / "pyproject.toml"
    elif variant == "legacy":
        (anchor / "pyproject.toml").write_text('[tool.flake8_lint]\nignore = ["X001"]\n', encoding="utf-8")
        config_flag = anchor / "pyproject.toml"
    elif variant == "both_same_dir":
        (anchor / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
        (anchor / "pyproject.toml").write_text("[tool.flakeforge]\nselect = []\n", encoding="utf-8")
    # variant == "none": no config file; config_flag names a missing path.

    return {"anchor": anchor, "sample": sample, "outside": outside, "config_flag": config_flag}


def _argv_and_cwd(mode: str, files: dict):
    """Return the ``(cwd, argv)`` for *mode* against a built variant.

    :param mode: One of :data:`MODES`.
    :param files: The mapping returned by :func:`_build_files`.
    :returns: A ``(cwd, argv)`` pair to drive :func:`flakeforge.cli.main`.
    """
    if mode == "cwd_inside":
        return files["anchor"], ["check", "--select", "X001", "."]
    if mode == "cwd_outside":
        return files["outside"], ["check", "--select", "X001", str(files["sample"])]
    if mode == "config_flag":
        return files["outside"], [
            "check",
            "--config",
            str(files["config_flag"]),
            "--select",
            "X001",
            str(files["sample"]),
        ]
    if mode == "no_config":
        return files["outside"], ["check", "--no-config", "--select", "X001", str(files["sample"])]
    raise AssertionError(f"unknown mode: {mode!r}")


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mode", MODES)
def test_config_precedence_matrix(variant, mode, tmp_path, monkeypatch, capsys) -> None:
    """Assert selected config, effective ``ignore``, and exit code per cell."""
    files = _build_files(variant, tmp_path)
    cwd, argv = _argv_and_cwd(mode, files)
    expect = EXPECT[variant][mode]
    monkeypatch.chdir(cwd)

    if not expect.get("error"):
        args = build_parser().parse_args(argv)
        config = _build_cli_config(args)
        assert config.ignore == expect["ignore"]
        if expect["config"] is None:
            assert config.config_path is None
        else:
            assert config.config_path is not None
            assert config.config_path.name == expect["config"]
        # Flush captured output so the warning counts below reflect only main().
        capsys.readouterr()

    exit_code = main(argv)
    assert exit_code == expect["exit"]

    err = capsys.readouterr().err
    for substring in expect.get("warnings", []):
        assert substring in err
    for substring in expect.get("forbid", []):
        assert substring not in err
    if "shadow" in expect:
        shadow_lines = [line for line in err.splitlines() if _SHADOW in line]
        assert len(shadow_lines) == expect["shadow"]


def test_section_less_pyproject_does_not_stop_upward_search(tmp_path, monkeypatch, capsys) -> None:
    """A section-less ``pyproject.toml`` is skipped, not treated as a match (C3).

    The nearest directory has a ``pyproject.toml`` with no ``flakeforge`` section,
    so discovery must keep walking up to the parent ``flakeforge.toml`` that
    governs the run.
    """
    parent = tmp_path / "proj"
    child = parent / "pkg"
    child.mkdir(parents=True)
    (parent / "flakeforge.toml").write_text('ignore = ["X001"]\n', encoding="utf-8")
    (child / "pyproject.toml").write_text("[tool.black]\nline-length = 100\n", encoding="utf-8")
    sample = child / "sample.py"
    sample.write_text(SAMPLE, encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.chdir(outside)
    argv = ["check", "--select", "X001", str(sample)]
    config = _build_cli_config(build_parser().parse_args(argv))
    assert config.config_path is not None
    assert config.config_path.name == "flakeforge.toml"
    assert config.ignore == ("X001",)
    assert main(argv) == 0
    assert capsys.readouterr().out.strip() == "Checked 1 file(s); no violations found."
