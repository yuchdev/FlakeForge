"""Fault-telemetry events: the event shape, sinks, sink loader and result adapter.

This module is self-contained and stdlib-only. It defines FlakeForge's own
:class:`FaultEvent` and :class:`FaultEventSink`; conformance with the sibling
DocSentinel project is by written spec (field names, types and vocabularies),
never by shared code. See ``docs/adr/0002-fault-telemetry-event-emission.md``.

Emission is opt-in and observational: it never alters a lint result or exit code.

The module also aggregates an event stream into a **summary document**
(:func:`summarize_events`): per-model and per-rule counts over the same events,
shaped like DocSentinel's JSON report (``schema_version`` / ``run`` / ``summary`` /
``rules`` / ``files``). ``telemetry_summary`` writes it for a run, and
:func:`read_events` loads a JSONL or SQLite archive so many runs can be summarised
offline (``flakeforge telemetry summary``).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, TextIO, Union

from .config import LintConfig
from .registry import RuleRegistry, _project_root_on_syspath

if TYPE_CHECKING:
    from .api import LintResult, RuleViolation

SCHEMA_VERSION = "1.0"
"""Version of the cross-tool event contract; a string, unrelated to the report's integer version."""

DOMAIN = "code"
TIER = "deterministic"
UNMAPPED_CLASS = "unmapped"

OUTCOME_PASS = "pass"
OUTCOME_REJECTED = "rejected"
OUTCOME_SUPPRESSED = "suppressed"
OUTCOME_ADVISORY = "advisory"
OUTCOME_ESCALATED = "escalated"
OUTCOMES = (
    OUTCOME_PASS,
    OUTCOME_REJECTED,
    OUTCOME_SUPPRESSED,
    OUTCOME_ADVISORY,
    OUTCOME_ESCALATED,
)
"""The shared outcome vocabulary. ``escalated`` is reserved and never produced here."""

WIRE_KEYS = (
    "ts",
    "domain",
    "class",
    "tier",
    "rule",
    "outcome",
    "model",
    "schema_version",
    "details",
)

# Rule code -> fault class. Kept in step with ``fault_classes.md``.
FAULT_CLASSES: dict[str, str] = {
    "X001": "broad_exception_handling",
    "X002": "broad_exception_handling",
    "X003": "import_structure",
    "X004": "swallowed_error",
    "X005": "missing_documentation",
    "X006": "import_structure",
    "X007": "missing_type_annotation",
    "X008": "misleading_type_annotation",
    "X009": "legacy_formatting",
    "X010": "swallowed_error",
    "X011": "annotation_style_violation",
    "X012": "annotation_style_violation",
    "X013": "resource_leak_risk",
    "X014": "untracked_work_marker",
    "X015": "stale_suppression",
}

DetailValue = Union[str, int, float, bool, None]

_LOGGER = logging.getLogger(__name__)


def utc_now_rfc3339() -> str:
    """Return the current UTC time as RFC-3339 with a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class FaultEvent:
    """One fault-telemetry event.

    :ivar ts: RFC-3339 UTC timestamp with a ``Z`` suffix.
    :ivar domain: Always ``"code"`` for FlakeForge.
    :ivar fault_class: Fault class; serialised under the wire key ``"class"``.
    :ivar tier: Always ``"deterministic"`` for FlakeForge.
    :ivar rule: The rule code, e.g. ``"X013"``.
    :ivar outcome: One of :data:`OUTCOMES`.
    :ivar model: Generator model id, or ``None``.
    :ivar schema_version: Cross-tool contract version string.
    :ivar details: Shallow JSON-primitive mapping. It must never carry source
        text, file contents or matched snippets: only codes, counts and
        base-relative display paths.
    """

    ts: str
    domain: str
    fault_class: str
    tier: str
    rule: str
    outcome: str
    model: Optional[str]
    schema_version: str = SCHEMA_VERSION
    details: Mapping[str, DetailValue] = field(default_factory=dict)


def to_wire_dict(event: FaultEvent) -> dict[str, object]:
    """Return *event* as its wire mapping, renaming ``fault_class`` to ``"class"``."""
    return {
        "ts": event.ts,
        "domain": event.domain,
        "class": event.fault_class,
        "tier": event.tier,
        "rule": event.rule,
        "outcome": event.outcome,
        "model": event.model,
        "schema_version": event.schema_version,
        "details": dict(event.details),
    }


class FaultEventSink(Protocol):
    """Structural contract for event consumers; implementers import nothing."""

    def emit(self, event: FaultEvent):
        """Consume one event."""
        pass

    def close(self):
        """Flush and release any held resources."""
        pass


class TelemetrySinkError(RuntimeError):
    """A ``telemetry_sink`` spec could not be resolved to a conforming sink."""


class NullSink:
    """The default sink: discards every event."""

    def emit(self, event: FaultEvent):
        """Discard *event*."""

    def close(self):
        """Do nothing."""


class LoggingSink:
    """Route each event through :mod:`logging` as one compact JSON message."""

    def __init__(self, logger: Optional[logging.Logger] = None, level: int = logging.INFO):
        """Create the sink.

        :param logger: Target logger; defaults to ``flakeforge.telemetry``.
        :param level: Level events are logged at.
        """
        self._logger = logger or logging.getLogger("flakeforge.telemetry")
        self._level = level

    def emit(self, event: FaultEvent):
        """Log *event* as JSON."""
        self._logger.log(self._level, "%s", json.dumps(to_wire_dict(event)))

    def close(self):
        """Do nothing; the logger is owned by the caller."""


class JsonlSink:
    """Write one compact JSON line per event to a text stream."""

    def __init__(self, stream: TextIO, *, owns_stream: bool = False):
        """Create the sink.

        :param stream: Destination text stream.
        :param owns_stream: Whether :meth:`close` closes *stream* (never for stdout).
        """
        self._stream = stream
        self._owns_stream = owns_stream

    def emit(self, event: FaultEvent):
        """Write and flush *event* as a JSON line."""
        self._stream.write(json.dumps(to_wire_dict(event), separators=(",", ":")) + "\n")
        self._stream.flush()

    def close(self):
        """Close the stream only when this sink owns it."""
        if self._owns_stream:
            self._stream.close()


class SqliteSink:
    """Insert one row per event into a ``fault_events`` SQLite table.

    Postgres and other databases are bring-your-own sinks: any class with
    conforming ``emit``/``close`` methods works, and FlakeForge adds no database
    client dependency.
    """

    def __init__(self, path: Union[str, Path]):
        """Open (creating if needed) the database at *path* and ensure the table."""
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS fault_events ("
            "ts TEXT, domain TEXT, class TEXT, tier TEXT, rule TEXT, "
            "outcome TEXT, model TEXT, schema_version TEXT, details TEXT)"
        )
        self._conn.commit()

    def emit(self, event: FaultEvent):
        """Insert *event* as one row, with ``details`` stored as JSON text."""
        row = to_wire_dict(event)
        self._conn.execute(
            "INSERT INTO fault_events VALUES (:ts,:domain,:class,:tier,:rule,"
            ":outcome,:model,:schema_version,:details)",
            {**row, "details": json.dumps(row["details"])},
        )
        self._conn.commit()

    def close(self):
        """Close the connection."""
        self._conn.close()


def build_sink(spec: str, *, project_root: Optional[Path] = None) -> FaultEventSink:
    """Build the sink selected by a ``telemetry_sink`` spec.

    ``""`` -> :class:`NullSink`; ``"stdout"``, ``"file:<path>"``,
    ``"sqlite:<path>"`` and ``"logging"`` select the reference sinks; any other
    string is a dotted import target for a custom sink (see
    :func:`_load_custom_sink`). ``file:``/``sqlite:`` paths are output artifacts,
    not root-confined input paths.

    :param spec: The ``telemetry_sink`` value.
    :param project_root: The config-vouched directory placed on ``sys.path`` while
        a custom sink imports; ``None`` loads nothing project-local.
    :returns: A sink satisfying :class:`FaultEventSink`.
    :raises TelemetrySinkError: If the spec cannot be resolved to a sink.
    """
    if not spec:
        return NullSink()
    if spec == "stdout":
        return JsonlSink(sys.stdout)
    if spec == "logging":
        return LoggingSink()
    if spec.startswith("file:"):
        return _open_file_sink(spec, spec[len("file:") :])
    if spec.startswith("sqlite:"):
        return _open_sqlite_sink(spec, spec[len("sqlite:") :])
    return _load_custom_sink(spec, project_root)


def _require_path(spec: str, raw: str) -> str:
    """Return *raw* or raise when a ``file:``/``sqlite:`` spec has no path."""
    if not raw:
        raise TelemetrySinkError(f"Invalid telemetry sink {spec!r}: a path is required")
    return raw


def _open_file_sink(spec: str, raw: str) -> JsonlSink:
    """Open *raw* for append and wrap it in an owning :class:`JsonlSink`."""
    path = _require_path(spec, raw)
    try:
        stream = open(path, "a", encoding="utf-8")
    except OSError as exc:
        raise TelemetrySinkError(f"Cannot open telemetry sink {spec!r}: {exc}") from exc
    return JsonlSink(stream, owns_stream=True)


def _open_sqlite_sink(spec: str, raw: str) -> SqliteSink:
    """Open the SQLite database at *raw*."""
    path = _require_path(spec, raw)
    try:
        return SqliteSink(path)
    except sqlite3.Error as exc:
        raise TelemetrySinkError(f"Cannot open telemetry sink {spec!r}: {exc}") from exc


def _load_custom_sink(spec: str, project_root: Optional[Path]) -> FaultEventSink:
    """Import a custom sink the same way ``rule_modules`` are loaded.

    The import runs with *project_root* first on ``sys.path`` only for its
    duration (contract C7), so loading a custom sink **executes code the target
    repository controls**, exactly like a project-local rule module. *spec* is a
    dotted module exposing a zero-arg ``build_sink()``, or ``module.attr`` /
    ``module:attr`` naming a zero-arg factory or class with ``emit``/``close``.

    :raises TelemetrySinkError: If the target cannot be imported, called, or does
        not expose callable ``emit`` and ``close``.
    """
    resolved_root = project_root.resolve() if project_root is not None else None
    location = f"base_dir {resolved_root}" if resolved_root is not None else "the existing sys.path"
    try:
        with _project_root_on_syspath(resolved_root):
            factory = _resolve_factory(spec)
            sink = factory()
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise TelemetrySinkError(f"Failed to load telemetry sink {spec!r} from {location}: {exc}") from exc
    for method in ("emit", "close"):
        if not callable(getattr(sink, method, None)):
            raise TelemetrySinkError(f"Telemetry sink {spec!r} does not define callable {method}()")
    return sink


def _resolve_factory(spec: str) -> Callable[[], FaultEventSink]:
    """Resolve *spec* to a zero-arg callable producing a sink."""
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
        return _get_callable(import_module(module_name), attr, spec)
    try:
        return _get_callable(import_module(spec), "build_sink", spec)
    except ModuleNotFoundError as exc:
        if exc.name != spec or "." not in spec:
            raise
    module_name, _, attr = spec.rpartition(".")
    return _get_callable(import_module(module_name), attr, spec)


def _get_callable(module, attr: str, spec: str) -> Callable[[], FaultEventSink]:
    """Return ``module.attr`` or raise :class:`AttributeError` if not callable."""
    target = getattr(module, attr, None)
    if target is None or not callable(target):
        raise AttributeError(f"{spec} does not define a callable {attr}")
    return target


def events_for_result(
    result: LintResult,
    registry: RuleRegistry,
    config: LintConfig,
    *,
    model: Optional[str],
    suppressed: Sequence[RuleViolation] = (),
    capture_run: bool = False,
    now: Callable[[], str] = utc_now_rfc3339,
) -> Iterator[FaultEvent]:
    """Yield fault events for an already-assembled *result*.

    Nothing is re-run: events are derived from ``result.violations``, the
    baseline-dropped *suppressed* findings, and the registry/config enablement.

    :param result: The finished lint result.
    :param registry: The resolved registry, used to find clean enabled rules.
    :param config: The validated configuration, used for select/ignore enablement.
    :param model: Generator model id; falsy becomes ``None``.
    :param suppressed: Violations dropped by the baseline filter (``suppressed``
        outcome). Fingerprints carry no rule code, so the call site supplies these.
    :param capture_run: ``True`` on a ``--write-baseline`` run, where captured
        violations are accepted and reported as ``advisory`` instead of ``rejected``.
    :param now: Timestamp factory, called once per run.
    :returns: An iterator of events; ``escalated`` is never produced.
    """
    timestamp = now()
    model_id = model or None
    violation_outcome = OUTCOME_ADVISORY if capture_run else OUTCOME_REJECTED

    def build(rule: str, outcome: str, details: Mapping[str, DetailValue]) -> FaultEvent:
        """Construct one event with the run-level constants applied."""
        return FaultEvent(
            ts=timestamp,
            domain=DOMAIN,
            fault_class=FAULT_CLASSES.get(rule, UNMAPPED_CLASS),
            tier=TIER,
            rule=rule,
            outcome=outcome,
            model=model_id,
            details=details,
        )

    for violation in result.violations:
        yield build(
            violation.code,
            violation_outcome,
            {"path": violation.filename, "line": violation.lineno},
        )
    for violation in suppressed:
        yield build(
            violation.code,
            OUTCOME_SUPPRESSED,
            {"path": violation.filename, "line": violation.lineno},
        )
    fired = {violation.code for violation in result.violations}
    fired.update(violation.code for violation in suppressed)
    for registration in registry.all():
        code = registration.code
        if registration.reserved or not registration.enabled or code in fired:
            continue
        if not _rule_selected(code, config):
            continue
        yield build(code, OUTCOME_PASS, {"files_checked": result.files_checked})


def _rule_selected(code: str, config: LintConfig) -> bool:
    """Return whether *code* survives the config's select/ignore prefixes."""
    if config.select and not code.startswith(config.select):
        return False
    return not code.startswith(config.ignore)


def emit_events(sink: FaultEventSink, events: Iterable[FaultEvent]):
    """Feed *events* to *sink* and always close it, never raising into the caller.

    Telemetry is observational: any sink or event-generation failure is logged at
    warning level and swallowed so it cannot change a lint result or exit code.
    """
    try:
        for event in events:
            sink.emit(event)
    except Exception as exc:  # noqa: X002
        _LOGGER.warning("Telemetry emission failed: %s", exc)
    finally:
        try:
            sink.close()
        except Exception as exc:  # noqa: X002
            _LOGGER.warning("Telemetry sink close failed: %s", exc)


SUMMARY_SCHEMA_VERSION = 1
"""Version of the summary document shape; an integer like a report ``schema_version``."""

SUMMARY_KEYS = ("schema_version", "event_schema_versions", "domains", "run", "summary", "rules", "files")
"""Top-level keys of a summary document, in order (mirrors DocSentinel's report sections)."""

_WIRE_STRING_KEYS = ("ts", "domain", "class", "tier", "rule", "outcome", "schema_version")
_FILES_CHECKED = "files_checked"
_PATH = "path"
_LINE = "line"


class TelemetrySourceError(RuntimeError):
    """A telemetry source (JSON lines file or SQLite database) could not be read as events."""


def summarize_events(events: Iterable[FaultEvent]) -> dict[str, object]:
    """Aggregate *events* into the summary document.

    The document mirrors the sections of DocSentinel's JSON report: ``run`` names
    the generator model (``model`` when every event agrees, plus a per-model count
    table), ``summary`` carries the totals and the ``by_rule`` / ``by_class`` /
    ``by_outcome`` statistics, ``rules`` lists which codes were evaluated, stayed
    clean or fired, and ``files`` groups the findings by display path. A *finding*
    is any event whose outcome is not ``pass`` (``rejected``, ``advisory``,
    ``suppressed`` or ``escalated``); ``by_outcome`` separates them. Runs are told
    apart by their shared timestamp, and ``files_checked`` is known only from a
    run's ``pass`` events, so it is ``None`` when any run had none. Output is
    deterministic: every table is key-sorted, files are path-sorted and problems
    keep event order. Only codes, counts, outcomes and display paths are carried,
    never source text.

    :param events: The events to aggregate; consumed once.
    :returns: A JSON-shaped mapping of ``dict`` / ``list`` / ``str`` / ``int`` / ``None``.
    """
    collected = tuple(events)
    findings = tuple(event for event in collected if event.outcome != OUTCOME_PASS)
    timestamps = sorted({event.ts for event in collected})
    models = _model_rows(collected, findings)
    files = _file_rows(findings)
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "event_schema_versions": sorted({event.schema_version for event in collected}),
        "domains": sorted({event.domain for event in collected}),
        "run": {
            "model": _single_model(collected),
            "models": models,
            "runs": len(timestamps),
            "first_ts": timestamps[0] if timestamps else None,
            "last_ts": timestamps[-1] if timestamps else None,
        },
        "summary": {
            "events": len(collected),
            "files_checked": _files_checked(collected),
            "files_with_problems": len(files),
            "findings": len(findings),
            "by_outcome": _sorted_counts(event.outcome for event in collected),
            "by_rule": _sorted_counts(event.rule for event in findings),
            "by_class": _sorted_counts(event.fault_class for event in findings),
        },
        "rules": _rule_section(collected, findings),
        "files": files,
    }


def _sorted_counts(values: Iterable[str]) -> dict[str, int]:
    """Count *values* into a key-sorted mapping so the output is deterministic."""
    counts = Counter(values)
    return {key: counts[key] for key in sorted(counts)}


def _single_model(events: Sequence[FaultEvent]) -> Optional[str]:
    """Return the one model every event carries, or ``None`` when unknown or mixed."""
    models = {event.model for event in events}
    if len(models) != 1:
        return None
    return next(iter(models))


def _model_rows(events: Sequence[FaultEvent], findings: Sequence[FaultEvent]) -> list[dict[str, object]]:
    """Return one ``{model, events, findings}`` row per model, sorted with ``None`` last."""
    events_by_model = Counter(event.model for event in events)
    findings_by_model = Counter(event.model for event in findings)
    known = sorted(model for model in events_by_model if model is not None)
    ordered: list[Optional[str]] = [*known, *([None] if None in events_by_model else [])]
    return [
        {"model": model, "events": events_by_model[model], "findings": findings_by_model.get(model, 0)}
        for model in ordered
    ]


def _files_checked(events: Sequence[FaultEvent]) -> Optional[int]:
    """Sum the per-run ``files_checked`` recorded on ``pass`` events, or ``None`` if any run lacks one."""
    per_run: dict[str, Optional[int]] = {}
    for event in events:
        current = per_run.setdefault(event.ts, None)
        if event.outcome != OUTCOME_PASS:
            continue
        value = event.details.get(_FILES_CHECKED)
        if isinstance(value, int) and not isinstance(value, bool):
            per_run[event.ts] = value if current is None else max(current, value)
    if not per_run or any(value is None for value in per_run.values()):
        return None
    return sum(value for value in per_run.values() if value is not None)


def _rule_section(events: Sequence[FaultEvent], findings: Sequence[FaultEvent]) -> dict[str, object]:
    """Build the ``rules`` section: evaluated count, clean and fired codes, and per-rule detail."""
    first_seen: dict[str, FaultEvent] = {}
    outcomes: dict[str, Counter[str]] = {}
    for event in events:
        first_seen.setdefault(event.rule, event)
        outcomes.setdefault(event.rule, Counter())[event.outcome] += 1
    fired = {event.rule for event in findings}
    codes = sorted(first_seen)
    detail = [
        {
            "rule": code,
            "fault_class": first_seen[code].fault_class,
            "tier": first_seen[code].tier,
            "events": sum(outcomes[code].values()),
            "findings": sum(count for outcome, count in outcomes[code].items() if outcome != OUTCOME_PASS),
            "by_outcome": {outcome: outcomes[code][outcome] for outcome in sorted(outcomes[code])},
        }
        for code in codes
    ]
    return {
        "evaluated": len(codes),
        "clean": [code for code in codes if code not in fired],
        "fired": [code for code in codes if code in fired],
        "detail": detail,
    }


def _file_rows(findings: Sequence[FaultEvent]) -> list[dict[str, object]]:
    """Group *findings* by their ``details.path`` into path-sorted file entries.

    A finding without a string ``path`` detail is counted in the totals but cannot be
    placed under a file, so it is left out of this section.
    """
    grouped: dict[str, list[dict[str, object]]] = {}
    domains: dict[str, str] = {}
    for event in findings:
        path = event.details.get(_PATH)
        if not isinstance(path, str):
            continue
        line = event.details.get(_LINE)
        grouped.setdefault(path, []).append(
            {
                "rule": event.rule,
                "fault_class": event.fault_class,
                "tier": event.tier,
                "outcome": event.outcome,
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "model": event.model,
            }
        )
        domains.setdefault(path, event.domain)
    return [
        {"path": path, "domain": domains[path], "problem_count": len(grouped[path]), "problems": grouped[path]}
        for path in sorted(grouped)
    ]


def render_summary(document: Mapping[str, object]) -> str:
    """Render a summary document as indented, ASCII-only JSON in section order."""
    return json.dumps(document, indent=2, ensure_ascii=True)


def probe_summary_path(path: str) -> str:
    """Check up front that the ``telemetry_summary`` target can be written.

    Mirrors the fail-fast open of the ``file:`` sink: the file is created when
    absent but an existing summary is **not** truncated, so a run that later fails
    leaves the previous document intact.

    :param path: The configured summary path (an output artifact, not root-confined).
    :returns: *path*, unchanged.
    :raises TelemetrySinkError: If *path* is empty or cannot be opened for writing.
    """
    if not path:
        raise TelemetrySinkError("Invalid telemetry summary path: a path is required")
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise TelemetrySinkError(f"Cannot open telemetry summary {path!r}: {exc}") from exc
    return path


def write_summary(path: str, events: Iterable[FaultEvent]):
    """Write the summary document for *events* to *path*, replacing any previous one.

    Like :func:`emit_events` this is observational: a failure is logged at warning
    level and swallowed so it can never change a lint result or exit code.
    """
    try:
        document = summarize_events(events)
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(render_summary(document) + "\n")
    except Exception as exc:  # noqa: X002
        _LOGGER.warning("Telemetry summary failed: %s", exc)


STATISTICS_FORMATS: tuple[str, ...] = ("text", "json", "github")
"""Formats the statistics file can be rendered in (``github`` is Markdown for ``$GITHUB_STEP_SUMMARY``)."""

_TOP_FILES = 10


def _ratio(numerator: int, denominator: Optional[int]) -> Optional[float]:
    """Return ``numerator / denominator`` rounded to 4 places, or ``None`` when undefined."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def compute_statistics(summary: Mapping[str, Any]) -> dict[str, object]:
    """Derive aggregate statistics from a summary document (see :func:`summarize_events`).

    Unlike the summary this carries no per-finding rows: it holds totals, shares and
    rates per rule / class / model, and the files with the most problems. Output is
    deterministic (ties break on name) and JSON-shaped.

    :param summary: A document produced by :func:`summarize_events`.
    :returns: A mapping with ``totals``, ``by_outcome``, ``by_class``, ``by_rule``,
        ``by_model`` and ``top_files`` sections.
    """
    totals = summary["summary"]
    run = summary["run"]
    findings = totals["findings"]
    files_checked = totals["files_checked"]
    by_rule = [
        {
            "rule": row["rule"],
            "fault_class": row["fault_class"],
            "events": row["events"],
            "findings": row["findings"],
            "hit_rate": _ratio(row["findings"], row["events"]),
            "share_of_findings": _ratio(row["findings"], findings),
        }
        for row in summary["rules"]["detail"]
    ]
    by_rule.sort(key=lambda row: (-row["findings"], row["rule"]))
    by_class = [
        {"fault_class": name, "findings": count, "share_of_findings": _ratio(count, findings)}
        for name, count in sorted(totals["by_class"].items(), key=lambda item: (-item[1], item[0]))
    ]
    files = sorted(summary["files"], key=lambda row: (-row["problem_count"], row["path"]))
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "model": run["model"],
        "totals": {
            "runs": run["runs"],
            "events": totals["events"],
            "files_checked": files_checked,
            "files_with_problems": totals["files_with_problems"],
            "findings": findings,
            "rules_evaluated": summary["rules"]["evaluated"],
            "rules_fired": len(summary["rules"]["fired"]),
            "rules_clean": len(summary["rules"]["clean"]),
            "findings_per_file": _ratio(findings, files_checked),
            "files_with_problems_ratio": _ratio(totals["files_with_problems"], files_checked),
        },
        "by_outcome": dict(totals["by_outcome"]),
        "by_class": by_class,
        "by_rule": by_rule,
        "by_model": [
            {**row, "hit_rate": _ratio(row["findings"], row["events"])} for row in run["models"]
        ],
        "top_files": [
            {"path": row["path"], "problem_count": row["problem_count"]} for row in files[:_TOP_FILES]
        ],
    }


def _pct(value: Optional[float]) -> str:
    """Format a 0-1 ratio as a percentage, or ``n/a`` when undefined."""
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _markdown_table(head: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """Render *rows* under *head* as a GitHub-flavoured Markdown table."""
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def render_statistics(statistics: Mapping[str, Any], output_format: str) -> str:
    """Render *statistics* as ``json``, plain ``text`` or ``github`` Markdown tables."""
    if output_format == "json":
        return json.dumps(statistics, indent=2, ensure_ascii=True)
    if output_format not in STATISTICS_FORMATS:
        raise ValueError(f"Unknown telemetry statistics format {output_format!r}; use one of {STATISTICS_FORMATS}")
    totals = statistics["totals"]
    model = statistics["model"] or "unknown"
    rule_rows = [
        (r["rule"], r["fault_class"], r["findings"], r["events"], _pct(r["hit_rate"]), _pct(r["share_of_findings"]))
        for r in statistics["by_rule"]
    ]
    rule_head = ("Rule", "Class", "Findings", "Events", "Hit rate", "Share")
    file_rows = [(f["path"], f["problem_count"]) for f in statistics["top_files"]]
    total_rows = [
        ("Model", model),
        ("Runs", totals["runs"]),
        ("Files checked", totals["files_checked"]),
        ("Files with problems", f"{totals['files_with_problems']} ({_pct(totals['files_with_problems_ratio'])})"),
        ("Findings", totals["findings"]),
        ("Findings per file", totals["findings_per_file"]),
        ("Rules fired / evaluated", f"{totals['rules_fired']} / {totals['rules_evaluated']}"),
    ]
    if output_format == "github":
        return "\n\n".join(
            [
                "## FlakeForge telemetry statistics",
                _markdown_table(("Metric", "Value"), total_rows),
                "### By rule",
                _markdown_table(rule_head, rule_rows),
                "### By outcome",
                _markdown_table(("Outcome", "Events"), list(statistics["by_outcome"].items())),
                f"### Top {len(file_rows)} files",
                _markdown_table(("Path", "Problems"), file_rows),
            ]
        )
    out = ["FlakeForge telemetry statistics", ""]
    out += [f"{label}: {value}" for label, value in total_rows]
    out += ["", "By rule:"]
    out += [
        f"  {code:<8} {klass:<22} findings={found:<7} events={events:<7} hit={hit:<8} share={share}"
        for code, klass, found, events, hit, share in rule_rows
    ]
    out += ["", "By outcome: " + ", ".join(f"{k} {v}" for k, v in statistics["by_outcome"].items())]
    out += ["", f"Top {len(file_rows)} files:"] + [f"  {count:>6}  {path}" for path, count in file_rows]
    return "\n".join(out)


def write_statistics(path: str, output_format: str, events: Iterable[FaultEvent]):
    """Write the statistics for *events* to *path*; observational like :func:`write_summary`."""
    try:
        text = render_statistics(compute_statistics(summarize_events(events)), output_format)
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
    except Exception as exc:  # noqa: X002
        _LOGGER.warning("Telemetry statistics failed: %s", exc)


def read_events(spec: str) -> Iterator[FaultEvent]:
    """Read events back from a telemetry source.

    *spec* is ``file:<path>`` or a bare path (JSON lines, one event per line, as
    the ``file:`` and ``stdout`` sinks write), or ``sqlite:<path>`` (the
    ``fault_events`` table the ``sqlite:`` sink fills). Rows are yielded in
    source order; blank lines are skipped.

    :param spec: The source to read.
    :returns: An iterator of events.
    :raises TelemetrySourceError: If the source is missing, unreadable, or holds a
        malformed event.
    """
    if spec.startswith("sqlite:"):
        return _read_sqlite_events(_require_source_path(spec, spec[len("sqlite:") :]))
    raw = spec[len("file:") :] if spec.startswith("file:") else spec
    return _read_jsonl_events(_require_source_path(spec, raw))


def _require_source_path(spec: str, raw: str) -> str:
    """Return *raw* or raise when a source spec names no path."""
    if not raw:
        raise TelemetrySourceError(f"Invalid telemetry source {spec!r}: a path is required")
    return raw


def _read_jsonl_events(path: str) -> Iterator[FaultEvent]:
    """Yield one event per non-blank JSON line of *path*."""
    try:
        with open(path, encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TelemetrySourceError(f"{path}:{number}: not a JSON event: {exc}") from exc
                yield event_from_wire(row, source=f"{path}:{number}")
    except OSError as exc:
        raise TelemetrySourceError(f"Cannot read telemetry source {path!r}: {exc}") from exc


def _read_sqlite_events(path: str) -> Iterator[FaultEvent]:
    """Yield every ``fault_events`` row of the SQLite database at *path* in insertion order."""
    if not os.path.isfile(path):
        raise TelemetrySourceError(f"Cannot read telemetry source {path!r}: no such file")
    try:
        with closing(sqlite3.connect(path)) as connection:
            rows = connection.execute(
                "SELECT ts, domain, class, tier, rule, outcome, model, schema_version, details "
                "FROM fault_events ORDER BY rowid"
            ).fetchall()
    except sqlite3.Error as exc:
        raise TelemetrySourceError(f"Cannot read telemetry source {path!r}: {exc}") from exc
    for number, row in enumerate(rows, start=1):
        record = dict(zip(WIRE_KEYS, row, strict=True))
        source = f"{path}#{number}"
        details = record["details"]
        if isinstance(details, str):
            try:
                record["details"] = json.loads(details)
            except json.JSONDecodeError as exc:
                raise TelemetrySourceError(f"{source}: details is not JSON: {exc}") from exc
        yield event_from_wire(record, source=source)


def event_from_wire(row: object, *, source: str = "event") -> FaultEvent:
    """Rebuild a :class:`FaultEvent` from its wire mapping (the inverse of :func:`to_wire_dict`).

    Every string field must be present as a string, ``model`` may be a string or
    ``null``, and ``details`` must be an object (``null`` or absent reads as empty).
    ``details`` values are copied as-is, not deep-validated.

    :param row: The decoded wire object.
    :param source: Where the row came from, for error messages.
    :returns: The reconstructed event.
    :raises TelemetrySourceError: If *row* does not have the wire shape.
    """
    if not isinstance(row, Mapping):
        raise TelemetrySourceError(f"{source}: event is not a JSON object")
    strings: dict[str, str] = {}
    for key in _WIRE_STRING_KEYS:
        value = row.get(key)
        if not isinstance(value, str):
            raise TelemetrySourceError(f"{source}: field {key!r} must be a string")
        strings[key] = value
    model = row.get("model")
    if model is not None and not isinstance(model, str):
        raise TelemetrySourceError(f"{source}: field 'model' must be a string or null")
    details = row.get("details")
    if details is None:
        details = {}
    if not isinstance(details, Mapping):
        raise TelemetrySourceError(f"{source}: field 'details' must be an object")
    return FaultEvent(
        ts=strings["ts"],
        domain=strings["domain"],
        fault_class=strings["class"],
        tier=strings["tier"],
        rule=strings["rule"],
        outcome=strings["outcome"],
        model=model,
        schema_version=strings["schema_version"],
        details=dict(details),
    )
