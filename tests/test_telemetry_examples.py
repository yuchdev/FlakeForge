"""Integration tests: run every ``src/examples/`` scenario with fault telemetry
enabled and compare the resulting summary document to a per-scenario
``expected_telemetry.json`` sidecar.

Complements ``tests/test_examples.py`` (which checks *violation codes* and exit
codes) by checking the telemetry *summary statistics* derived from the same
scenario: the total number of findings and the per-rule-code breakdown
(``telemetry.summarize_events``'s ``summary.findings`` / ``summary.by_rule``,
written by ``--telemetry-summary``). Each scenario runs exactly as
``test_examples.py`` runs it -- same ``paths``, same ``rule_modules`` handling,
same working directory, read from the existing ``expected.toml`` -- with
``--telemetry-sink``/``--telemetry-summary`` added, so a scenario needs no
changes to pick up telemetry coverage; it only needs an
``expected_telemetry.json`` sidecar naming the expected counts.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from flakeforge.cli import main

EXAMPLES_DIR = Path(__file__).parents[1] / "src" / "examples"


def _discover_telemetry_scenarios() -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    """Return ``(scenario_dir, run_config, expected_telemetry)`` triples.

    A scenario is included only when it carries both the existing
    ``expected.toml`` (read here only for its ``paths``/``rule_modules`` run
    parameters -- the violation-code assertions are ``test_examples.py``'s job)
    and a new ``expected_telemetry.json`` sidecar.
    """
    results: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    for candidate in sorted(EXAMPLES_DIR.iterdir()):
        if not candidate.is_dir():
            continue
        telemetry_path = candidate / "expected_telemetry.json"
        run_config_path = candidate / "expected.toml"
        if not telemetry_path.is_file() or not run_config_path.is_file():
            continue
        run_config: dict[str, Any] = tomllib.loads(run_config_path.read_text(encoding="utf-8"))
        expected: dict[str, Any] = json.loads(telemetry_path.read_text(encoding="utf-8"))
        results.append((candidate, run_config, expected))
    return results


_SCENARIOS = _discover_telemetry_scenarios()
_SCENARIO_IDS = [scenario_dir.name for scenario_dir, _, _ in _SCENARIOS]


@pytest.mark.parametrize("scenario_dir,run_config,expected", _SCENARIOS, ids=_SCENARIO_IDS)
def test_example_telemetry_summary(
    scenario_dir: Path,
    run_config: dict[str, Any],
    expected: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the scenario with telemetry enabled and diff its summary against the sidecar.

    Scenario: an in-process ``flakeforge check --telemetry-sink file:... --telemetry-summary ...``
    from inside the scenario directory, mirroring ``test_examples._run_cli_check``'s
    config discovery and ``rule_modules`` path handling exactly, so this run resolves
    the identical configuration as the violation-code assertion.
    Boundaries: real CLI ``main()``; one temp events/summary pair per run.
    On failure, first check: the scenario's ``expected_telemetry.json`` counts first
    (did the scenario's app.py change?), then a built-in rule's behavior or its
    ``FAULT_CLASSES`` mapping in ``fault_classes.md``/``telemetry.py``.
    """
    paths: list[str] = run_config["paths"]
    rule_modules: list[str] = run_config.get("rule_modules", [])
    events_path = tmp_path / "events.jsonl"
    summary_path = tmp_path / "summary.json"

    if rule_modules:
        monkeypatch.syspath_prepend(str(scenario_dir))
    monkeypatch.chdir(scenario_dir)
    main(
        [
            "check",
            "--telemetry-sink",
            f"file:{events_path}",
            "--telemetry-summary",
            str(summary_path),
            *paths,
        ]
    )

    assert summary_path.is_file(), f"Scenario {scenario_dir.name!r}: no telemetry summary was written"
    document = json.loads(summary_path.read_text(encoding="utf-8"))

    actual_findings = document["summary"]["findings"]
    actual_by_rule = document["summary"]["by_rule"]
    assert actual_findings == expected["findings"], (
        f"Scenario {scenario_dir.name!r}: expected {expected['findings']} telemetry finding(s), "
        f"got {actual_findings} (by_rule={actual_by_rule})"
    )
    assert actual_by_rule == expected["by_rule"], (
        f"Scenario {scenario_dir.name!r}: expected by_rule {expected['by_rule']}, got {actual_by_rule}"
    )


def test_every_scenario_directory_has_a_telemetry_sidecar() -> None:
    """[Guard] Every ``expected.toml`` scenario also carries an ``expected_telemetry.json``.

    Scenario: list every scenario ``test_examples.py`` discovers and assert each
    has the sidecar this module reads, so a new example added without one is
    caught immediately rather than silently skipping telemetry coverage.
    Boundaries: filesystem discovery only, no run.
    On failure, first check: a newly added ``src/examples/<name>/`` fixture is
    missing its ``expected_telemetry.json`` sidecar.
    """
    scenario_dirs = {
        candidate.name
        for candidate in EXAMPLES_DIR.iterdir()
        if candidate.is_dir() and (candidate / "expected.toml").is_file()
    }
    covered = {scenario_dir.name for scenario_dir, _, _ in _SCENARIOS}
    missing = scenario_dirs - covered
    assert not missing, f"Scenario(s) missing expected_telemetry.json: {sorted(missing)}"
