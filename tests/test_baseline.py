"""Tests for the baseline file: fingerprinting, load/validate, write, and the
engine-owned suppression applied by :func:`flakeforge.api.lint_paths`."""

import json
import os
from pathlib import Path

import pytest

from flakeforge import lint_paths
from flakeforge.api import format_json, format_text
from flakeforge.baseline import (
    BASELINE_VERSION,
    BaselineEntry,
    BaselineError,
    assign_entries,
    compute_fingerprint,
    dumps_baseline,
    load_baseline,
    normalize_line,
    write_baseline,
)
from flakeforge.config import ConfigValidationError, LintConfig

_TWO_BARE_EXCEPTS = (
    "def a():\n"
    "    try:\n"
    "        run()\n"
    "    except:\n"
    "        pass\n"
    "\n"
    "def b():\n"
    "    try:\n"
    "        run()\n"
    "    except:\n"
    "        pass\n"
)


def _project(tmp_path: Path, source: str) -> tuple[Path, LintConfig]:
    """Write ``m.py`` under a project dir and return it with an X001-only config."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "m.py").write_text(source, encoding="utf-8")
    config = LintConfig(
        select=("X001",),
        apply_baseline=True,
        base_dir=project,
        config_path=project / "flakeforge.toml",
    )
    return project, config


# --------------------------------------------------------------------------- #
# Fingerprint + normalization                                                 #
# --------------------------------------------------------------------------- #


def test_compute_fingerprint_is_stable_and_hex() -> None:
    first = compute_fingerprint("X001", "pkg/m.py", "    except:")
    second = compute_fingerprint("X001", "pkg/m.py", "    except:")
    assert first == second
    assert len(first) == 64
    assert int(first, 16) >= 0  # pure hex digest


def test_normalize_line_strips_surrounding_whitespace_keeps_interior() -> None:
    # Leading/trailing spaces AND tabs are stripped; interior text is verbatim.
    assert normalize_line("\t   except:  \t") == "except:"
    assert normalize_line("a = \t b") == "a = \t b"
    # Re-indenting a line does not change its fingerprint.
    assert compute_fingerprint("X001", "m.py", "    except:") == compute_fingerprint("X001", "m.py", "except:")


def test_fingerprint_field_separator_prevents_collision() -> None:
    # Without a field separator "X1"+"a" and "X"+"1a" would concatenate equally.
    assert compute_fingerprint("X1", "a", "line") != compute_fingerprint("X", "1a", "line")


def test_fingerprint_changes_when_line_text_changes() -> None:
    assert compute_fingerprint("X001", "m.py", "    except:") != compute_fingerprint(
        "X001", "m.py", "    except Exception:"
    )


def test_assign_entries_indexes_duplicates_in_order() -> None:
    entries = assign_entries(["a", "b", "a", "a"])
    assert entries == [
        BaselineEntry("a", 0),
        BaselineEntry("b", 0),
        BaselineEntry("a", 1),
        BaselineEntry("a", 2),
    ]


# --------------------------------------------------------------------------- #
# Write / read / byte-stability                                               #
# --------------------------------------------------------------------------- #


def test_write_read_round_trip(tmp_path: Path) -> None:
    entries = [BaselineEntry("a" * 64, 0), BaselineEntry("b" * 64, 1)]
    path = tmp_path / "baseline.json"
    write_baseline(path, entries)
    assert load_baseline(path) == frozenset(entries)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == BASELINE_VERSION


def test_write_is_byte_stable_regardless_of_input_order() -> None:
    a = BaselineEntry("a" * 64, 0)
    b = BaselineEntry("b" * 64, 0)
    assert dumps_baseline([a, b]) == dumps_baseline([b, a])
    # De-duplicates identical entries and ends with a single trailing newline.
    assert dumps_baseline([a, a]) == dumps_baseline([a])
    assert dumps_baseline([a]).endswith("}\n")


# --------------------------------------------------------------------------- #
# Malformed baseline files -> BaselineError                                    #
# --------------------------------------------------------------------------- #


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(BaselineError):
        load_baseline(tmp_path / "nope.json")


def test_load_bad_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(BaselineError):
        load_baseline(path)


@pytest.mark.parametrize(
    "document",
    [
        [],  # not an object
        {"version": 2, "entries": []},  # wrong version
        {"version": BASELINE_VERSION, "entries": {}},  # entries not a list
        {"version": BASELINE_VERSION, "entries": [42]},  # entry not an object
        {"version": BASELINE_VERSION, "entries": [{"fingerprint": "short", "occurrence": 0}]},  # bad fingerprint
        {"version": BASELINE_VERSION, "entries": [{"fingerprint": "a" * 64, "occurrence": -1}]},  # bad occurrence
        {"version": BASELINE_VERSION, "entries": [{"fingerprint": "a" * 64, "occurrence": True}]},  # bool occurrence
    ],
)
def test_load_wrong_shape_raises(tmp_path: Path, document: object) -> None:
    path = tmp_path / "b.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BaselineError):
        load_baseline(path)


# --------------------------------------------------------------------------- #
# Config surface                                                               #
# --------------------------------------------------------------------------- #


def test_config_baseline_rejects_absolute_path() -> None:
    with pytest.raises(ConfigValidationError):
        LintConfig.from_mapping({"baseline": "/etc/baseline.json"})


def test_config_baseline_accepts_relative_and_defaults_empty() -> None:
    assert LintConfig.from_mapping({}).baseline == ""
    assert LintConfig.from_mapping({"baseline": "ci/baseline.json"}).baseline == "ci/baseline.json"


# --------------------------------------------------------------------------- #
# Engine suppression (lint_paths)                                             #
# --------------------------------------------------------------------------- #


def _write_current_baseline(project: Path, config: LintConfig, name: str = "baseline.json") -> Path:
    """Lint *project* and persist the current fingerprints to a baseline file."""
    result = lint_paths([project], config=config)
    path = project / name
    write_baseline(path, result.fingerprints)
    return path


def test_baseline_suppresses_known_violations(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    with_baseline = config.merge(baseline="baseline.json")
    result = lint_paths([project], config=with_baseline)
    assert result.violations == ()
    assert result.ok is True
    assert result.baseline_fixed == ()


def test_baseline_is_not_applied_by_default(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    result = lint_paths([project], config=LintConfig(
        select=("X001",),
        baseline="baseline.json",
        base_dir=project,
        config_path=project / "flakeforge.toml",
    ))
    assert len(result.violations) == 2


def test_baseline_survives_line_shift(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    # Insert unrelated lines ABOVE the violations; the line numbers move but the
    # normalized line text is unchanged, so both stay suppressed (decision D2).
    shifted = '"""Module docstring added later."""\nimport os\n\n' + _TWO_BARE_EXCEPTS
    (project / "m.py").write_text(shifted, encoding="utf-8")
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    assert result.violations == ()


def test_editing_flagged_line_resurfaces_violation(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    # Change the first bare except's text: its fingerprint no longer matches, so
    # it re-surfaces as a new violation while the second stays suppressed.
    edited = _TWO_BARE_EXCEPTS.replace("    except:\n        pass\n", "    except:  # kept\n        pass\n", 1)
    (project / "m.py").write_text(edited, encoding="utf-8")
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    assert [v.lineno for v in result.violations] == [4]
    # One baseline entry (occurrence 0) no longer matches -> reported as fixed.
    assert len(result.baseline_fixed) == 1


def test_duplicate_lines_use_occurrence_index(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    # Remove the SECOND identical function; one bare except remains. It matches
    # occurrence 0, and occurrence 1 is now stale (fixed). Nothing new surfaces.
    single = "def a():\n    try:\n        run()\n    except:\n        pass\n"
    (project / "m.py").write_text(single, encoding="utf-8")
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    assert result.violations == ()
    assert len(result.baseline_fixed) == 1


def test_new_violation_is_reported_when_baselined_ones_are_hidden(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    # Append a THIRD bare except (different text is not needed: occurrence 2 is new).
    extended = _TWO_BARE_EXCEPTS + ("\ndef c():\n    try:\n        run()\n    except:\n        pass\n")
    (project / "m.py").write_text(extended, encoding="utf-8")
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    # Two occurrences were baselined; the third is new.
    assert len(result.violations) == 1
    assert result.baseline_fixed == ()


def test_baseline_resolves_relative_to_base_dir_regardless_of_cwd(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    # Baseline lives beside the config; base_dir-relative resolution (C6).
    _write_current_baseline(project, config)
    with_baseline = config.merge(baseline="baseline.json")
    sibling = tmp_path / "elsewhere"
    sibling.mkdir()
    outcomes = []
    cwd = Path.cwd()
    for run_dir in (tmp_path, sibling):
        try:
            os.chdir(run_dir)
            outcomes.append(lint_paths([project], config=with_baseline).violations)
        finally:
            os.chdir(cwd)
    assert outcomes[0] == outcomes[1] == ()


def test_missing_baseline_file_raises_from_lint_paths(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    with pytest.raises(BaselineError):
        lint_paths([project], config=config.merge(baseline="absent.json"))


# --------------------------------------------------------------------------- #
# Formatter surfacing of fixed entries                                        #
# --------------------------------------------------------------------------- #


def test_format_text_reports_fixed_count(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    (project / "m.py").write_text("x = 1\n", encoding="utf-8")  # both violations gone
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    text = format_text(result)
    assert "no violations found" in text
    assert "2 baseline entries no longer match" in text


def test_format_json_lists_fixed_entries(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    _write_current_baseline(project, config)
    (project / "m.py").write_text("x = 1\n", encoding="utf-8")
    result = lint_paths([project], config=config.merge(baseline="baseline.json"))
    payload = json.loads(format_json(result))
    assert payload["schema_version"] == 1
    assert len(payload["baseline"]["fixed"]) == 2
    assert all(set(entry) == {"fingerprint", "occurrence"} for entry in payload["baseline"]["fixed"])


def test_format_json_omits_baseline_key_without_fixed_entries(tmp_path: Path) -> None:
    project, config = _project(tmp_path, _TWO_BARE_EXCEPTS)
    result = lint_paths([project], config=config)
    assert "baseline" not in json.loads(format_json(result))


def test_x015_unused_noqa_can_be_baselined(tmp_path: Path) -> None:
    """An engine-emitted X015 flows through fingerprinting and can be baselined."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "m.py").write_text("x = 1  # noqa\n", encoding="utf-8")
    config = LintConfig(
        select=("X015",),
        apply_baseline=True,
        base_dir=project,
        config_path=project / "flakeforge.toml",
    )
    first = lint_paths([project], config=config)
    assert [v.code for v in first.violations] == ["X015"]
    write_baseline(project / "baseline.json", first.fingerprints)
    second = lint_paths([project], config=config.merge(baseline="baseline.json"))
    assert second.violations == ()
    assert second.ok is True


def test_load_rejects_non_regular_file(tmp_path) -> None:
    with pytest.raises(BaselineError, match="not a regular file"):
        load_baseline(tmp_path)


def test_load_rejects_oversized_file(tmp_path, monkeypatch) -> None:
    target = tmp_path / "big.json"
    target.write_text('{"version": 1, "entries": []}\n', encoding="utf-8")
    monkeypatch.setattr("flakeforge.baseline.MAX_BASELINE_BYTES", 4)
    with pytest.raises(BaselineError, match="exceeds"):
        load_baseline(target)


def test_load_rejects_non_hex_fingerprint(tmp_path) -> None:
    target = tmp_path / "b.json"
    target.write_text(json.dumps({"version": 1, "entries": [{"fingerprint": "z" * 64, "occurrence": 0}]}), encoding="utf-8")
    with pytest.raises(BaselineError, match="invalid fingerprint"):
        load_baseline(target)
