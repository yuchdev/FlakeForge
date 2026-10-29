"""Tests for fault telemetry: event shape, sinks, loader, adapter and emission."""

import io
import json
import sqlite3
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from flakeforge import lint_paths
from flakeforge.baseline import write_baseline
from flakeforge.catalog import builtin_registrations
from flakeforge.cli import main
from flakeforge.config import LintConfig
from flakeforge.registry import resolve_registry
from flakeforge.telemetry import (
    FAULT_CLASSES,
    OUTCOMES,
    SCHEMA_VERSION,
    WIRE_KEYS,
    FaultEvent,
    JsonlSink,
    LoggingSink,
    NullSink,
    SqliteSink,
    TelemetrySinkError,
    build_sink,
    emit_events,
    events_for_result,
    to_wire_dict,
)

_BARE_EXCEPT = "def a():\n    try:\n        run()\n    except:\n        pass\n"


def _event(**overrides) -> FaultEvent:
    """Build a sample event."""
    values = {
        "ts": "2026-01-01T00:00:00Z",
        "domain": "code",
        "fault_class": "swallowed_error",
        "tier": "deterministic",
        "rule": "X004",
        "outcome": "rejected",
        "model": None,
        "details": {"path": "m.py", "line": 3},
    }
    values.update(overrides)
    return FaultEvent(**values)


def _project(tmp_path: Path, source: str = _BARE_EXCEPT, **config) -> tuple[Path, LintConfig]:
    """Write ``m.py`` under a project dir with an X001-only config."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "m.py").write_text(source, encoding="utf-8")
    base = LintConfig(select=("X001", "X002"), base_dir=project, config_path=project / "flakeforge.toml")
    return project, base.merge(**config)


def test_conformance_tripwire_literals() -> None:
    """[Unit] Pin the literals the cross-tool spec fixes.

    Scenario: compare FlakeForge's own constants with the spec's expected values.
    Boundaries: no DocSentinel import; literal values only.
    On failure, first check: the spec doc and ADR 0002 before changing a literal.
    """
    assert SCHEMA_VERSION == "1.0"
    assert WIRE_KEYS == ("ts", "domain", "class", "tier", "rule", "outcome", "model", "schema_version", "details")
    assert OUTCOMES == ("pass", "rejected", "suppressed", "advisory", "escalated")
    assert tuple(to_wire_dict(_event())) == WIRE_KEYS
    wire = to_wire_dict(_event())
    assert wire["domain"] == "code" and wire["tier"] == "deterministic"


def test_every_builtin_code_has_a_fault_class() -> None:
    """[Unit] Every built-in code maps to a class and the doc table lists it.

    Scenario: iterate the built-in registrations.
    Boundaries: reads fault_classes.md from the source tree.
    On failure, first check: add the row to FAULT_CLASSES and fault_classes.md.
    """
    doc = (Path(__file__).parents[1] / "src" / "flakeforge" / "fault_classes.md").read_text(encoding="utf-8")
    for registration in builtin_registrations():
        assert FAULT_CLASSES[registration.code] != "unmapped"
        assert f"| `{registration.code}` " in doc
        assert f"`{FAULT_CLASSES[registration.code]}`" in doc


def test_wire_dict_renames_class_and_event_is_frozen() -> None:
    """[Unit] to_wire_dict emits ``class`` and the event is immutable.

    Scenario: serialise an event and try to mutate it.
    Boundaries: no I/O.
    On failure, first check: the fault_class -> "class" rename.
    """
    event = _event()
    wire = to_wire_dict(event)
    assert wire["class"] == "swallowed_error" and "fault_class" not in wire
    assert wire["model"] is None
    with pytest.raises(FrozenInstanceError):
        event.rule = "X001"  # type: ignore[misc]


def test_jsonl_sink_leaves_borrowed_stream_open_and_closes_owned() -> None:
    """[Unit] JsonlSink writes compact lines and respects stream ownership.

    Scenario: emit to a borrowed and an owned stream, then close.
    Boundaries: in-memory streams.
    On failure, first check: owns_stream handling in close().
    """
    borrowed = io.StringIO()
    sink = JsonlSink(borrowed)
    sink.emit(_event())
    sink.close()
    assert not borrowed.closed
    assert json.loads(borrowed.getvalue())["class"] == "swallowed_error"
    owned = io.StringIO()
    JsonlSink(owned, owns_stream=True).close()
    assert owned.closed


def test_logging_and_null_sinks(caplog) -> None:
    """[Unit] LoggingSink logs wire JSON; NullSink discards.

    Scenario: emit one event through each.
    Boundaries: stdlib logging only.
    On failure, first check: logger name flakeforge.telemetry.
    """
    import logging

    with caplog.at_level(logging.INFO, logger="flakeforge.telemetry"):
        LoggingSink().emit(_event())
    assert json.loads(caplog.records[0].getMessage())["rule"] == "X004"
    NullSink().emit(_event())
    NullSink().close()


def test_sqlite_sink_writes_one_row_per_event(tmp_path) -> None:
    """[Integration] SqliteSink stores details as JSON text.

    Scenario: emit two events into a temp database and read them back.
    Boundaries: real sqlite3 file in tmp_path.
    On failure, first check: the fault_events schema and details serialisation.
    """
    db = tmp_path / "t.db"
    sink = SqliteSink(db)
    sink.emit(_event())
    sink.emit(_event(outcome="pass"))
    sink.close()
    rows = sqlite3.connect(db).execute("SELECT class, outcome, details FROM fault_events").fetchall()
    assert [r[1] for r in rows] == ["rejected", "pass"]
    assert json.loads(rows[0][2]) == {"path": "m.py", "line": 3}


def test_build_sink_builtin_specs(tmp_path) -> None:
    """[Unit] build_sink maps each built-in spec to its sink.

    Scenario: build every keyword spec and file/sqlite paths (absolute allowed).
    Boundaries: no custom-module import.
    On failure, first check: the spec table in the ADR.
    """
    assert isinstance(build_sink(""), NullSink)
    stdout_sink = build_sink("stdout")
    assert isinstance(stdout_sink, JsonlSink)
    stdout_sink.close()
    assert not sys.stdout.closed
    assert isinstance(build_sink("logging"), LoggingSink)
    file_sink = build_sink(f"file:{tmp_path / 'e.jsonl'}")
    file_sink.close()
    sqlite_sink = build_sink(f"sqlite:{tmp_path / 'e.db'}")
    sqlite_sink.close()
    with pytest.raises(TelemetrySinkError):
        build_sink("file:")
    with pytest.raises(TelemetrySinkError):
        build_sink(f"file:{tmp_path / 'missing' / 'e.jsonl'}")


def _write_sink_module(root: Path, name: str = "my_sinks") -> None:
    """Write a project-local custom sink module with a build_sink factory."""
    (root / f"{name}.py").write_text(
        "class Counting:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "    def emit(self, event):\n"
        "        self.n += 1\n"
        "    def close(self):\n"
        "        pass\n"
        "\n"
        "def build_sink():\n"
        "    return Counting()\n",
        encoding="utf-8",
    )


def test_custom_sink_loads_from_project_root_only(tmp_path) -> None:
    """[Integration] A custom sink loads via project_root and not without it.

    Scenario: module lives only in tmp_path; load with and without a root.
    Boundaries: mirrors the rule_modules trust boundary (C7).
    On failure, first check: _project_root_on_syspath usage in the loader.
    """
    _write_sink_module(tmp_path, "tel_sink_mod")
    sink = build_sink("tel_sink_mod", project_root=tmp_path)
    assert type(sink).__name__ == "Counting"
    sys.modules.pop("tel_sink_mod", None)
    assert str(tmp_path) not in sys.path
    with pytest.raises(TelemetrySinkError, match="tel_sink_mod"):
        build_sink("tel_sink_mod", project_root=None)


def test_custom_sink_class_target_and_bad_targets(tmp_path) -> None:
    """[Unit] Class targets resolve; non-conforming or missing targets error.

    Scenario: dotted class, colon form, a non-sink callable, and a missing module.
    Boundaries: tmp module on project_root.
    On failure, first check: _resolve_factory and the emit/close check.
    """
    _write_sink_module(tmp_path, "tel_sink_cls")
    assert type(build_sink("tel_sink_cls.Counting", project_root=tmp_path)).__name__ == "Counting"
    assert type(build_sink("tel_sink_cls:build_sink", project_root=tmp_path)).__name__ == "Counting"
    with pytest.raises(TelemetrySinkError, match="emit"):
        build_sink("collections:OrderedDict")
    with pytest.raises(TelemetrySinkError):
        build_sink("no_such_sink_module_xyz", project_root=tmp_path)


def test_adapter_outcomes(tmp_path) -> None:
    """[Unit] events_for_result maps violations, baseline drops and clean rules.

    Scenario: lint a project with one X001 violation, with and without a baseline.
    Boundaries: no sink; the adapter is fed an assembled result.
    On failure, first check: outcome mapping in events_for_result.
    """
    project, config = _project(tmp_path)
    registry = resolve_registry(include_entry_points=False)
    result = lint_paths([project], config=config, registry=registry)
    events = list(events_for_result(result, registry, config, model="m1", now=lambda: "T"))
    by_outcome = {(e.rule, e.outcome) for e in events}
    assert ("X001", "rejected") in by_outcome and ("X002", "pass") in by_outcome
    assert {e.model for e in events} == {"m1"} and {e.ts for e in events} == {"T"}
    assert all(e.rule in ("X001", "X002") for e in events)
    assert "escalated" not in {e.outcome for e in events}
    flat = json.dumps([dict(e.details) for e in events])
    assert "except" not in flat and "pass" not in flat
    advisory = list(events_for_result(result, registry, config, model=None, capture_run=True))
    assert {(e.rule, e.outcome) for e in advisory} >= {("X001", "advisory")}
    assert advisory[0].model is None


def test_unmapped_fallback() -> None:
    """[Unit] A code without a mapping row maps to ``unmapped``.

    Scenario: build events for a custom code.
    Boundaries: hand-built result.
    On failure, first check: UNMAPPED_CLASS fallback.
    """
    from flakeforge.api import LintResult, RuleViolation

    registry = resolve_registry(include_entry_points=False)
    result = LintResult(violations=(RuleViolation("a.py", 1, 0, "ACME001", "m"),), files_checked=1)
    first = next(events_for_result(result, registry, LintConfig(), model=None))
    assert first.fault_class == "unmapped"


def test_emission_via_lint_paths_and_baseline_suppression(tmp_path) -> None:
    """[Integration] lint_paths emits rejected, suppressed and pass events.

    Scenario: a file sink on a run, then again with that violation baselined.
    Boundaries: real files in tmp_path.
    On failure, first check: the emission block in api.lint_paths.
    """
    project, config = _project(tmp_path)
    out = tmp_path / "events.jsonl"
    registry = resolve_registry(include_entry_points=False)
    result = lint_paths([project], config=config.merge(telemetry_sink=f"file:{out}"), registry=registry)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {(r["rule"], r["outcome"]) for r in rows} == {("X001", "rejected"), ("X002", "pass")}
    assert not result.ok

    baseline = tmp_path / "b.json"
    write_baseline(baseline, result.fingerprints)
    out.unlink()
    lint_paths(
        [project],
        config=config.merge(telemetry_sink=f"file:{out}", baseline=str(baseline)),
        registry=registry,
    )
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert ("X001", "suppressed") in {(r["rule"], r["outcome"]) for r in rows}


def test_disabled_telemetry_is_inert(tmp_path, capsys) -> None:
    """[Unit] With no sink configured nothing is written and results are identical.

    Scenario: lint with default config and compare with a sink run's result.
    Boundaries: stdout captured.
    On failure, first check: the empty-spec early skip in lint_paths.
    """
    project, config = _project(tmp_path)
    plain = lint_paths([project], config=config)
    assert capsys.readouterr().out == ""
    with_sink = lint_paths([project], config=config.merge(telemetry_sink="stdout"))
    capsys.readouterr()
    assert plain == with_sink


def test_sink_failure_is_swallowed(tmp_path) -> None:
    """[Mock] A sink raising during emit/close never reaches the caller.

    Scenario: emit_events with a sink whose methods raise.
    Boundaries: in-process fake sink.
    On failure, first check: emit_events exception handling.
    """

    class Boom:
        def emit(self, event):
            raise RuntimeError("boom")

        def close(self):
            raise RuntimeError("close boom")

    emit_events(Boom(), [_event()])


def test_cli_telemetry_flags_exit_codes(tmp_path, monkeypatch, capsys) -> None:
    """[Integration] CLI flags emit events, keep exit codes, and a bad spec exits 2.

    Scenario: run check with stdout sink, a bad sink, and --write-baseline.
    Boundaries: real CLI main() against tmp_path.
    On failure, first check: _run_check wiring and TelemetrySinkError mapping.
    """
    project, _ = _project(tmp_path)
    monkeypatch.chdir(project)
    code = main(["check", "--no-config", "--select", "X001", "--telemetry-sink", "stdout",
                 "--generated-by-model", "gpt-x", "m.py"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert code == 1
    assert json.loads(lines[0])["model"] == "gpt-x"

    assert main(["check", "--no-config", "--telemetry-sink", "bad_mod_zzz", "m.py"]) == 2
    assert "telemetry sink" in capsys.readouterr().err

    capture = tmp_path / "cap.jsonl"
    code = main(["check", "--no-config", "--select", "X001", "--telemetry-sink", f"file:{capture}",
                 "--write-baseline", str(tmp_path / "bl.json"), "m.py"])
    assert code == 0
    outcomes = {json.loads(line)["outcome"] for line in capture.read_text(encoding="utf-8").splitlines()}
    assert outcomes == {"advisory"}


def test_config_keys_and_show(tmp_path, monkeypatch, capsys) -> None:
    """[Integration] Both keys parse from TOML (absolute paths allowed) and show origins.

    Scenario: write flakeforge.toml with the keys and run config show.
    Boundaries: real config discovery in tmp_path.
    On failure, first check: _CONFIG_SCHEMA entries and cli override keys.
    """
    (tmp_path / "flakeforge.toml").write_text(
        f'telemetry_sink = "sqlite:{tmp_path / "x.db"}"\ngenerated_by_model = "m"\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["config", "show"]) == 0
    shown = capsys.readouterr().out
    assert "telemetry_sink" in shown and "generated_by_model" in shown
