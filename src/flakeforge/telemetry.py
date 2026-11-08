"""Fault-telemetry events: the event shape, sinks, sink loader and result adapter.

This module is self-contained and stdlib-only. It defines FlakeForge's own
:class:`FaultEvent` and :class:`FaultEventSink`; conformance with the sibling
DocSentinel project is by written spec (field names, types and vocabularies),
never by shared code. See ``docs/adr/0002-fault-telemetry-event-emission.md``.

Emission is opt-in and observational: it never alters a lint result or exit code.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, TextIO, Union

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

#: Rule code -> fault class. Kept in step with ``fault_classes.md``.
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
        ...

    def close(self):
        """Flush and release any held resources."""
        ...


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
