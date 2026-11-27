"""Tests for the telemetry summary: aggregation, source readers, and the config/CLI surface."""

import json
from pathlib import Path

import pytest

from flakeforge import lint_paths
from flakeforge.cli import main
from flakeforge.config import CONFIG_KEYS, LintConfig
from flakeforge.registry import resolve_registry
from flakeforge.telemetry import (
    SUMMARY_KEYS,
    SUMMARY_SCHEMA_VERSION,
    FaultEvent,
    JsonlSink,
    SqliteSink,
    TelemetrySinkError,
    TelemetrySourceError,
    event_from_wire,
    probe_summary_path,
    read_events,
    render_summary,
    summarize_events,
    to_wire_dict,
)

_BARE_EXCEPT = "def a():\n    try:\n        run()\n    except:\n        pass\n"


def _event(**overrides) -> FaultEvent:
    """Build a sample event; overrides replace individual fields."""
    values = {
        "ts": "T1",
        "domain": "code",
        "fault_class": "swallowed_error",
        "tier": "deterministic",
        "rule": "X004",
        "outcome": "rejected",
        "model": "m1",
        "details": {"path": "a.py", "line": 9},
    }
    values.update(overrides)
    return FaultEvent(**values)


def _stream() -> list[FaultEvent]:
    """Two runs (``T1`` with model ``m1``, ``T2`` without a model) over two files."""
    broad = {"fault_class": "broad_exception_handling"}
    return [
        _event(rule="X001", details={"path": "b.py", "line": 2}, **broad),
        _event(outcome="suppressed", details={"path": "a.py", "line": 7}),
        _event(),
        _event(rule="X002", outcome="pass", details={"files_checked": 3}, **broad),
        _event(ts="T2", model=None, rule="X001", details={"path": "b.py", "line": 2}, **broad),
        _event(ts="T2", model=None, rule="X002", outcome="pass", details={"files_checked": 4}, **broad),
    ]


def _project(tmp_path: Path, **config) -> tuple[Path, LintConfig]:
    """Write ``m.py`` with a bare except under a project dir with an X001/X002-only config."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "m.py").write_text(_BARE_EXCEPT, encoding="utf-8")
    base = LintConfig(select=("X001", "X002"), base_dir=project, config_path=project / "flakeforge.toml")
    return project, base.merge(**config)


def test_summary_document_shape_tripwire() -> None:
    """[Unit] Pin the section and key layout of the summary document.

    Scenario: summarise a small stream and compare key tuples section by section.
    Boundaries: no I/O; the layout mirrors DocSentinel's JSON report sections.
    On failure, first check: SUMMARY_KEYS, SUMMARY_SCHEMA_VERSION and the README example.
    """
    document = summarize_events(_stream())
    assert SUMMARY_SCHEMA_VERSION == 1
    assert SUMMARY_KEYS == ("schema_version", "event_schema_versions", "domains", "run", "summary", "rules", "files")
    assert tuple(document) == SUMMARY_KEYS
    assert document["schema_version"] == 1
    assert tuple(document["run"]) == ("model", "models", "runs", "first_ts", "last_ts")
    assert tuple(document["summary"]) == (
        "events", "files_checked", "files_with_problems", "findings", "by_outcome", "by_rule", "by_class",
    )
    assert tuple(document["rules"]) == ("evaluated", "clean", "fired", "detail")
    assert tuple(document["rules"]["detail"][0]) == ("rule", "fault_class", "tier", "events", "findings", "by_outcome")
    assert tuple(document["files"][0]) == ("path", "domain", "problem_count", "problems")
    assert tuple(document["files"][0]["problems"][0]) == ("rule", "fault_class", "tier", "outcome", "line", "model")
    assert json.loads(render_summary(document)) == document


def test_summary_counts_models_rules_outcomes_and_files() -> None:
    """[Unit] The statistics count per model, per rule, per class, per outcome and per file.

    Scenario: a mixed two-run stream with a known and an unknown model and a suppressed finding.
    Boundaries: no I/O; a finding is every non-pass event.
    On failure, first check: summarize_events and its _model_rows/_rule_section/_file_rows helpers.
    """
    document = summarize_events(_stream())
    run, summary, rules = document["run"], document["summary"], document["rules"]
    assert document["event_schema_versions"] == ["1.0"] and document["domains"] == ["code"]
    assert run["model"] is None  # mixed models
    assert run["models"] == [
        {"model": "m1", "events": 4, "findings": 3},
        {"model": None, "events": 2, "findings": 1},
    ]
    assert (run["runs"], run["first_ts"], run["last_ts"]) == (2, "T1", "T2")
    assert summary["events"] == 6 and summary["findings"] == 4
    assert summary["files_checked"] == 7 and summary["files_with_problems"] == 2
    assert summary["by_outcome"] == {"pass": 2, "rejected": 3, "suppressed": 1}
    assert summary["by_rule"] == {"X001": 2, "X004": 2}
    assert summary["by_class"] == {"broad_exception_handling": 2, "swallowed_error": 2}
    assert rules["evaluated"] == 3 and rules["clean"] == ["X002"] and rules["fired"] == ["X001", "X004"]
    x004 = next(row for row in rules["detail"] if row["rule"] == "X004")
    assert x004 == {
        "rule": "X004",
        "fault_class": "swallowed_error",
        "tier": "deterministic",
        "events": 2,
        "findings": 2,
        "by_outcome": {"rejected": 1, "suppressed": 1},
    }
    assert [entry["path"] for entry in document["files"]] == ["a.py", "b.py"]
    a_py = document["files"][0]
    assert a_py["domain"] == "code" and a_py["problem_count"] == 2
    assert [(p["line"], p["outcome"]) for p in a_py["problems"]] == [(7, "suppressed"), (9, "rejected")]
    assert [p["model"] for p in document["files"][1]["problems"]] == ["m1", None]


def test_summary_single_model_unknown_files_and_empty_stream() -> None:
    """[Unit] Edge cases: one shared model, a run without pass events, no events at all.

    Scenario: three small streams.
    Boundaries: no I/O.
    On failure, first check: _single_model and _files_checked.
    """
    uniform = summarize_events([_event(), _event(rule="X001", outcome="pass", details={"files_checked": 2})])
    assert uniform["run"]["model"] == "m1" and uniform["summary"]["files_checked"] == 2

    no_pass = summarize_events([_event(), _event(ts="T2", outcome="pass", details={"files_checked": 5})])
    assert no_pass["summary"]["files_checked"] is None  # run T1 recorded no pass event
    assert summarize_events([_event(outcome="pass", details={"files_checked": True})])["summary"]["files_checked"] is None

    empty = summarize_events([])
    assert empty["run"] == {"model": None, "models": [], "runs": 0, "first_ts": None, "last_ts": None}
    assert empty["summary"]["events"] == 0 and empty["summary"]["files_checked"] is None
    assert empty["rules"] == {"evaluated": 0, "clean": [], "fired": [], "detail": []}
    assert empty["files"] == [] and empty["event_schema_versions"] == [] and empty["domains"] == []


def test_summary_tolerates_pathless_findings_and_odd_lines() -> None:
    """[Unit] A finding without a path is counted but not filed; a non-int line reads as null.

    Scenario: findings with empty details and with a string line.
    Boundaries: no I/O.
    On failure, first check: _file_rows.
    """
    document = summarize_events([_event(details={}), _event(details={"path": "a.py", "line": "x"})])
    assert document["summary"]["findings"] == 2 and document["summary"]["by_rule"] == {"X004": 2}
    assert document["summary"]["files_with_problems"] == 1
    assert document["files"][0]["problems"][0]["line"] is None


def test_render_summary_is_ascii_only() -> None:
    """[Unit] render_summary escapes non-ASCII so the document is safe for any log.

    Scenario: a model id with a non-ASCII letter.
    Boundaries: no I/O.
    On failure, first check: ensure_ascii in render_summary.
    """
    text = render_summary(summarize_events([_event(model="mödel")]))
    assert "\\u00f6" in text and "ö" not in text
    assert json.loads(text)["run"]["model"] == "mödel"


def test_read_events_jsonl_round_trip_and_bare_path(tmp_path) -> None:
    """[Integration] Events written by JsonlSink read back identical, via ``file:`` or a bare path.

    Scenario: write two events plus blank lines, read with both spellings.
    Boundaries: real file in tmp_path.
    On failure, first check: _read_jsonl_events and event_from_wire.
    """
    path = tmp_path / "events.jsonl"
    events = [_event(), _event(ts="T2", model=None, outcome="pass", details={"files_checked": 1})]
    with path.open("w", encoding="utf-8") as stream:
        sink = JsonlSink(stream)
        for event in events:
            sink.emit(event)
        stream.write("\n\n")
    assert list(read_events(f"file:{path}")) == events
    assert list(read_events(str(path))) == events


def test_read_events_sqlite_round_trip(tmp_path) -> None:
    """[Integration] Events written by SqliteSink read back identical and in insertion order.

    Scenario: two events, one without a model, into a temp database.
    Boundaries: real sqlite3 file in tmp_path.
    On failure, first check: _read_sqlite_events column order against WIRE_KEYS.
    """
    db = tmp_path / "events.db"
    events = [_event(), _event(model=None, outcome="suppressed", details={"path": "b.py", "line": 1})]
    sink = SqliteSink(db)
    for event in events:
        sink.emit(event)
    sink.close()
    assert list(read_events(f"sqlite:{db}")) == events


def test_read_events_source_errors(tmp_path) -> None:
    """[Unit] Missing, malformed or non-conforming sources raise TelemetrySourceError.

    Scenario: missing files, an empty spec path, a bad JSON line, a non-database file, bad rows.
    Boundaries: tmp_path only; a missing sqlite path must not be created.
    On failure, first check: read_events and event_from_wire validation.
    """
    with pytest.raises(TelemetrySourceError, match="Cannot read"):
        list(read_events(str(tmp_path / "missing.jsonl")))
    with pytest.raises(TelemetrySourceError, match="a path is required"):
        list(read_events("file:"))
    with pytest.raises(TelemetrySourceError, match="a path is required"):
        list(read_events("sqlite:"))
    missing_db = tmp_path / "missing.db"
    with pytest.raises(TelemetrySourceError, match="no such file"):
        list(read_events(f"sqlite:{missing_db}"))
    assert not missing_db.exists()

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(to_wire_dict(_event())) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(TelemetrySourceError, match=r"bad\.jsonl:2: not a JSON event"):
        list(read_events(str(bad)))
    not_db = tmp_path / "text.db"
    not_db.write_text("not a database\n", encoding="utf-8")
    with pytest.raises(TelemetrySourceError, match="Cannot read"):
        list(read_events(f"sqlite:{not_db}"))

    good = to_wire_dict(_event())
    assert event_from_wire(good) == _event()
    assert event_from_wire({**good, "details": None}).details == {}
    with pytest.raises(TelemetrySourceError, match="not a JSON object"):
        event_from_wire(["x"])
    with pytest.raises(TelemetrySourceError, match="'rule' must be a string"):
        event_from_wire({**good, "rule": 4})
    with pytest.raises(TelemetrySourceError, match="'model' must be a string or null"):
        event_from_wire({**good, "model": 1})
    with pytest.raises(TelemetrySourceError, match="'details' must be an object"):
        event_from_wire({**good, "details": [1]})


def test_lint_paths_writes_summary_matching_the_sink(tmp_path) -> None:
    """[Integration] telemetry_summary writes the document for the same events the sink receives.

    Scenario: sink plus summary, then summary alone, then neither.
    Boundaries: real files in tmp_path; lint result unchanged by telemetry.
    On failure, first check: the emission block in api.lint_paths.
    """
    project, config = _project(tmp_path, generated_by_model="m1")
    registry = resolve_registry(include_entry_points=False)
    events_path, summary_path = tmp_path / "events.jsonl", tmp_path / "summary.json"
    result = lint_paths(
        [project],
        config=config.merge(telemetry_sink=f"file:{events_path}", telemetry_summary=str(summary_path)),
        registry=registry,
    )
    assert not result.ok
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    document = json.loads(summary_path.read_text(encoding="utf-8"))
    assert document["summary"]["events"] == len(rows) == 2
    assert document["run"]["model"] == "m1" and document["summary"]["by_rule"] == {"X001": 1}
    assert document["summary"]["files_checked"] == 1 and document["rules"]["clean"] == ["X002"]
    assert document["files"][0]["path"] == "m.py" and document["files"][0]["problems"][0]["line"] == 4

    summary_path.unlink()
    alone = lint_paths([project], config=config.merge(telemetry_summary=str(summary_path)), registry=registry)
    assert alone == result
    assert json.loads(summary_path.read_text(encoding="utf-8")) == document

    summary_path.unlink()
    assert lint_paths([project], config=config, registry=registry) == result
    assert not summary_path.exists()


def test_summary_path_is_probed_up_front_without_truncating(tmp_path) -> None:
    """[Unit] An unwritable summary path fails fast; a writable one keeps its old content.

    Scenario: a path in a missing directory, an empty path, and an existing file.
    Boundaries: tmp_path only.
    On failure, first check: probe_summary_path.
    """
    project, config = _project(tmp_path)
    with pytest.raises(TelemetrySinkError, match="telemetry summary"):
        lint_paths([project], config=config.merge(telemetry_summary=str(tmp_path / "nope" / "s.json")))
    with pytest.raises(TelemetrySinkError, match="a path is required"):
        probe_summary_path("")
    existing = tmp_path / "old.json"
    existing.write_text("old", encoding="utf-8")
    assert probe_summary_path(str(existing)) == str(existing)
    assert existing.read_text(encoding="utf-8") == "old"


def test_cli_check_summary_flag_and_config_key(tmp_path, monkeypatch, capsys) -> None:
    """[Integration] --telemetry-summary writes the document, keeps the exit code, and is a config key.

    Scenario: a check run with the flag, then config show with the key in flakeforge.toml.
    Boundaries: real CLI main() against tmp_path.
    On failure, first check: _build_cli_config / _cli_override_keys and _CONFIG_SCHEMA.
    """
    project, _ = _project(tmp_path)
    monkeypatch.chdir(project)
    summary = tmp_path / "summary.json"
    code = main(["check", "--no-config", "--select", "X001", "--telemetry-summary", str(summary),
                 "--generated-by-model", "gpt-x", "m.py"])
    assert code == 1
    assert "X001" in capsys.readouterr().out
    document = json.loads(summary.read_text(encoding="utf-8"))
    assert document["run"]["model"] == "gpt-x" and document["summary"]["by_rule"] == {"X001": 1}

    assert main(["check", "--no-config", "--telemetry-summary", str(tmp_path / "nope" / "s.json"), "m.py"]) == 2
    assert "telemetry summary" in capsys.readouterr().err

    assert "telemetry_summary" in CONFIG_KEYS
    (project / "flakeforge.toml").write_text(f'telemetry_summary = "{summary.as_posix()}"\n', encoding="utf-8")
    assert main(["config", "show"]) == 0
    assert "telemetry_summary" in capsys.readouterr().out


def test_cli_telemetry_summary_command(tmp_path, monkeypatch, capsys) -> None:
    """[Integration] ``telemetry summary`` aggregates sources; bad input and formats exit 2.

    Scenario: record events with a check run, then summarise them as text and JSON.
    Boundaries: real CLI main() against tmp_path.
    On failure, first check: _telemetry_summary and _render_telemetry_summary_text.
    """
    project, _ = _project(tmp_path)
    monkeypatch.chdir(project)
    events = tmp_path / "events.jsonl"
    assert main(["check", "--no-config", "--select", "X001,X002", "--telemetry-sink", f"file:{events}",
                 "--generated-by-model", "gpt-x", "m.py"]) == 1
    capsys.readouterr()

    assert main(["telemetry", "summary", str(events)]) == 0
    text = capsys.readouterr().out
    assert "events:   2 in 1 run(s)" in text and "models:   gpt-x (2)" in text
    assert "files:    1 checked, 1 with problems" in text and "findings: 1 (rejected 1)" in text
    assert "rule  class                     findings" in text
    assert "X001  broad_exception_handling         1" in text

    assert main(["telemetry", "summary", "--output-format", "json", f"file:{events}", str(events)]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == 1 and document["summary"]["events"] == 4
    assert document["run"]["models"] == [{"model": "gpt-x", "events": 4, "findings": 2}]

    clean = tmp_path / "clean.jsonl"
    clean.write_text(
        json.dumps(to_wire_dict(_event(outcome="pass", details={"files_checked": 1}))) + "\n", encoding="utf-8"
    )
    assert main(["telemetry", "summary", str(clean)]) == 0
    assert capsys.readouterr().out.rstrip().endswith("No findings.")

    assert main(["telemetry", "summary", str(tmp_path / "missing.jsonl")]) == 2
    assert "Cannot read telemetry source" in capsys.readouterr().err
    assert main(["telemetry"]) == 2
    assert "requires a subcommand: summary" in capsys.readouterr().err
    assert main(["telemetry", "summary", "--output-format", "github", str(events)]) == 2
    assert "telemetry summary supports only text and json" in capsys.readouterr().err


def test_write_summary_failure_is_swallowed(tmp_path, caplog) -> None:
    """[Unit] A summary that cannot be written is logged at warning level, never raised.

    Scenario: the target path is a directory.
    Boundaries: tmp_path only; mirrors emit_events' observational contract.
    On failure, first check: write_summary exception handling.
    """
    import logging

    from flakeforge.telemetry import write_summary

    with caplog.at_level(logging.WARNING, logger="flakeforge.telemetry"):
        write_summary(str(tmp_path), [_event()])
    assert any("Telemetry summary failed" in record.getMessage() for record in caplog.records)


def test_read_events_sqlite_rejects_corrupt_details(tmp_path) -> None:
    """[Integration] A ``fault_events`` row whose details column is not JSON is reported with its row number.

    Scenario: insert one good row via SqliteSink, then one with a bad details column.
    Boundaries: real sqlite3 file in tmp_path.
    On failure, first check: _read_sqlite_events details decoding.
    """
    import sqlite3
    from contextlib import closing

    db = tmp_path / "events.db"
    sink = SqliteSink(db)
    sink.emit(_event())
    sink.close()
    with closing(sqlite3.connect(db)) as connection:
        connection.execute(
            "INSERT INTO fault_events VALUES ('T', 'code', 'c', 'deterministic', 'X001', 'pass', NULL, '1.0', '{bad')"
        )
        connection.commit()
    with pytest.raises(TelemetrySourceError, match=r"events\.db#2: details is not JSON"):
        list(read_events(f"sqlite:{db}"))
