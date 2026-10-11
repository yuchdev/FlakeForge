"""Authoritative public API and shared engine for flakeforge."""

from __future__ import annotations

import ast
import io
import json
import tokenize
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Protocol, Union

from . import baseline as baseline_mod
from .baseline import BaselineEntry
from .config import LintConfig, validate_config
from .discovery import discover_python_files, path_matches_any
from .registry import RuleRegistry, resolve_registry

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

UNUSED_NOQA_CODE = "X015"
"""Built-in code the engine emits for a ``# noqa`` directive that suppressed nothing."""

UNUSED_NOQA_MESSAGE = "Unused `# noqa` directive; remove it or the code it no longer suppresses."
"""Static X015 message. Deliberately names no user code or source text (security)."""


@dataclass(frozen=True)
class RuleViolation:
    """A single rule violation located in a source file.

    :ivar filename: File the violation was found in.
    :ivar lineno: 1-based line number of the violation.
    :ivar col_offset: 0-based column offset of the violation.
    :ivar code: Rule code that produced the violation.
    :ivar message: Human-readable description of the violation.
    """

    filename: str
    lineno: int
    col_offset: int
    code: str
    message: str


@dataclass(frozen=True)
class LintResult:
    """Aggregate outcome of linting one or more files.

    :ivar violations: All violations found, in emission order.
    :ivar files_checked: Number of files that were linted.
    :ivar registered_rules: ``(code, description)`` pairs for every rule in the
        resolved registry, ordered by code. Populated by :func:`lint_paths` so
        registry-aware formatters (SARIF) can list all known rules without a
        second registry lookup; defaults to empty for hand-built results. It
        is not part of the base JSON output, but supplies the per-code
        descriptions when statistics are requested.
    :ivar baseline_fixed: Baseline entries that matched nothing on this run
        ("fixed"), sorted, empty unless a baseline was applied. The engine never
        fails a run over these; text output shows their count and JSON lists them
        (additive keys), while ``github``/``sarif`` omit them.
    :ivar fingerprints: One :class:`~flakeforge.baseline.BaselineEntry` per
        reported violation (before any baseline suppression), in C9 order. This
        is what ``--write-baseline`` persists; defaults to empty for hand-built
        results.
    """

    violations: tuple[RuleViolation, ...]
    files_checked: int
    registered_rules: tuple[tuple[str, str], ...] = ()
    baseline_fixed: tuple[BaselineEntry, ...] = ()
    fingerprints: tuple[BaselineEntry, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the run produced no violations."""
        return not self.violations


@dataclass(frozen=True)
class RuleContext:
    """Inputs handed to each rule for a single module.

    :ivar tree: Parsed AST of the module under analysis.
    :ivar filename: Display name of the module being linted.
    :ivar source: Original source text, when available, for line inspection.
    """

    tree: ast.AST
    filename: str
    source: Optional[str]


class Rule(Protocol):
    """Structural type implemented by every runnable rule.

    :ivar code: The rule's unique code.
    :ivar description: Human-readable summary of the rule.
    """

    code: str
    description: str

    def check(self, context: RuleContext) -> Iterable[RuleViolation]:
        """Yield the violations this rule finds in *context*."""
        ...


class RuleExecutionError(RuntimeError):
    """Raised when a rule provider fails during execution."""


def check_tree(
    tree: ast.AST,
    filename: str,
    source: Optional[str] = None,
    *,
    apply_noqa: bool = True,
    validate_selectors: bool = True,
    config: Optional[LintConfig] = None,
    registry=None,
    emit_unused_noqa: bool = True,
) -> tuple[RuleViolation, ...]:
    """Run the resolved rule registry over a parsed module.

    Callers may pass a pre-resolved *registry* to control provider loading and
    avoid repeated discovery work across multiple files.

    :param emit_unused_noqa: When true (the default), the engine additionally
        emits the built-in :data:`UNUSED_NOQA_CODE` (X015) for every ``# noqa``
        directive that suppressed nothing on this run. The Flake8 adapter passes
        ``False`` because Flake8 owns ``# noqa`` there; see
        :func:`_unused_noqa_violations` for the full semantics.
    """

    effective_config = config or LintConfig()
    effective_registry = registry or resolve_registry(
        rule_modules=effective_config.rule_modules,
        include_entry_points=True,
        project_root=effective_config.rule_module_root,
    )
    validated_config = (
        validate_config(effective_config, effective_registry.known_codes()) if validate_selectors else effective_config
    )
    context = RuleContext(tree=tree, filename=filename, source=source)
    violations: list[RuleViolation] = []
    # Codes covered (suppressed by a noqa directive or dropped by
    # ``per_file_ignores``) per source line, so X015 can tell a used directive
    # from an unused one. A per-file-ignored violation counts as covering the
    # directive on its line (see the seam in :func:`_is_per_file_ignored`).
    covered_by_line: dict[int, set[str]] = {}

    for registration in effective_registry.enabled_rules():
        if not _is_rule_enabled(registration.code, validated_config):
            continue
        try:
            emitted = tuple(registration.rule.check(context))
        except (
            AttributeError,
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            RuntimeError,
        ) as exc:  # pragma: no cover - defensive surface
            provider = registration.provider or "<unknown provider>"
            raise RuleExecutionError(f"Rule {registration.code} from {provider} failed: {exc}") from exc
        for violation in emitted:
            if _is_per_file_ignored(violation, validated_config):
                covered_by_line.setdefault(violation.lineno, set()).add(violation.code)
                continue
            if _is_noqa_suppressed(
                violation,
                context.source,
                validated_config,
                apply_noqa=apply_noqa,
            ):
                covered_by_line.setdefault(violation.lineno, set()).add(violation.code)
                continue
            violations.append(violation)

    if emit_unused_noqa:
        violations.extend(
            _unused_noqa_violations(
                context,
                validated_config,
                effective_registry,
                covered_by_line,
                apply_noqa=apply_noqa,
            )
        )

    return tuple(violations)


def check_file(
    path: Union[str, Path],
    *,
    apply_noqa: bool = True,
    validate_selectors: bool = True,
    config: Optional[LintConfig] = None,
    registry=None,
) -> tuple[RuleViolation, ...]:
    """Parse a Python file and run the resolved registry against it.

    Callers may pass a pre-resolved *registry* to reuse provider discovery
    across repeated file checks.
    """
    file_path = Path(path)
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))
    return check_tree(
        tree,
        str(file_path),
        source,
        apply_noqa=apply_noqa,
        validate_selectors=validate_selectors,
        config=config,
        registry=registry,
    )


def check_source(
    source: str,
    *,
    filename: str = "<string>",
    apply_noqa: bool = True,
    validate_selectors: bool = True,
    config: Optional[LintConfig] = None,
    registry=None,
    rules: Optional[Sequence[Rule]] = None,
) -> tuple[RuleViolation, ...]:
    """Parse source text and lint it with either an explicit registry or rules."""
    tree = ast.parse(source, filename=filename)
    effective_registry = registry
    if effective_registry is None and rules is not None:
        explicit_registry = RuleRegistry()
        for rule in rules:
            explicit_registry.register(rule, provider="flakeforge.check_source")
        effective_registry = explicit_registry
    return check_tree(
        tree,
        filename,
        source,
        apply_noqa=apply_noqa,
        validate_selectors=validate_selectors,
        config=config,
        registry=effective_registry,
    )


def lint_paths(
    paths: Optional[Sequence[Union[str, Path]]] = None,
    *,
    config: Optional[LintConfig] = None,
    registry=None,
) -> LintResult:
    """Discover Python files under *paths* and lint them.

    Callers may pass a pre-resolved *registry* when linting many files to avoid
    repeated provider-loading overhead.
    """

    effective_config = config or LintConfig()
    effective_registry = registry or resolve_registry(
        rule_modules=effective_config.rule_modules,
        include_entry_points=True,
        project_root=effective_config.rule_module_root,
    )
    validated_config = validate_config(effective_config, effective_registry.known_codes())
    target_paths = _resolve_target_paths(paths, validated_config)
    if paths:
        file_targets = tuple(path for path in target_paths if path.is_file())
        dir_targets = tuple(path for path in target_paths if path.is_dir())
        files = discover_python_files(dir_targets, config=validated_config)
        if file_targets:
            files = tuple(
                sorted(
                    {
                        *files,
                        *discover_python_files(
                            file_targets,
                            config=validated_config.merge(include=()),
                        ),
                    }
                )
            )
    else:
        files = discover_python_files(target_paths, config=validated_config)
    root_dir = (validated_config.base_dir or Path.cwd()).resolve()
    violations: list[RuleViolation] = []
    source_lines_by_display: dict[str, list[str]] = {}
    for file_path in files:
        display_name = _display_filename(file_path, root_dir)
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
        file_violations = check_tree(
            tree,
            str(file_path),
            source,
            config=validated_config,
            registry=effective_registry,
        )
        if file_violations:
            source_lines_by_display[display_name] = source.splitlines()
        violations.extend(replace(violation, filename=display_name) for violation in file_violations)
    registered_rules = tuple((registration.code, registration.description) for registration in effective_registry.all())
    reported, baseline_fixed, fingerprints = _apply_baseline(violations, source_lines_by_display, validated_config)
    return LintResult(
        violations=reported,
        files_checked=len(files),
        registered_rules=registered_rules,
        baseline_fixed=baseline_fixed,
        fingerprints=fingerprints,
    )


def _apply_baseline(
    violations: Sequence[RuleViolation],
    source_lines_by_display: dict[str, list[str]],
    config: LintConfig,
) -> tuple[tuple[RuleViolation, ...], tuple[BaselineEntry, ...], tuple[BaselineEntry, ...]]:
    """Fingerprint *violations* and drop any already recorded in the baseline.

    Suppression is engine-owned (repo convention): rules never see baselines. It
    runs last -- after select/ignore, ``per_file_ignores`` and ``# noqa`` -- so a
    baseline only ever drops a violation that would otherwise be reported (plan
    contract C1). Violations are fingerprinted in the deterministic C9 order so
    occurrence indices are stable (decision D2).

    :param violations: The reported violations, with base-relative display names.
    :param source_lines_by_display: Split source lines keyed by display filename,
        used to fingerprint each violation's source line.
    :param config: The validated effective configuration.
    :returns: ``(reported, fixed, fingerprints)`` where *reported* is the
        surviving violations, *fixed* is the sorted baseline entries that matched
        nothing, and *fingerprints* is one entry per reported violation before
        suppression (what ``--write-baseline`` persists).
    """
    ordered = _sorted_violations(violations)
    fingerprints = [
        baseline_mod.compute_fingerprint(
            violation.code,
            violation.filename,
            _violation_line_text(violation, source_lines_by_display),
        )
        for violation in ordered
    ]
    entries = baseline_mod.assign_entries(fingerprints)
    all_entries = tuple(entries)

    baseline_path = _resolve_baseline_path(config)
    if baseline_path is None or not config.apply_baseline:
        return tuple(violations), (), all_entries

    known = baseline_mod.load_baseline(baseline_path)
    reported: list[RuleViolation] = []
    matched: set[BaselineEntry] = set()
    for violation, entry in zip(ordered, entries, strict=True):
        if entry in known:
            matched.add(entry)
        else:
            reported.append(violation)
    fixed = tuple(sorted(known - matched))
    return tuple(reported), fixed, all_entries


def _violation_line_text(
    violation: RuleViolation,
    source_lines_by_display: dict[str, list[str]],
) -> str:
    """Return the source line *violation* points at, or ``""`` when unavailable."""
    lines = source_lines_by_display.get(violation.filename)
    if lines is None:
        return ""
    index = violation.lineno - 1
    if 0 <= index < len(lines):
        return lines[index]
    return ""


def _resolve_baseline_path(config: LintConfig) -> Optional[Path]:
    """Resolve the effective baseline path, or ``None`` when none is configured.

    An empty ``baseline`` means "no baseline". A config-file value is relative and
    resolves against ``base_dir`` (plan contract C6); the CLI resolves its
    ``--baseline`` to an absolute path before merging, so an absolute value is
    used verbatim and stays cwd-relative in origin.
    """
    raw = config.baseline
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    base = config.base_dir or Path.cwd()
    return base / candidate


def format_text(result: LintResult, *, statistics: bool = False) -> str:
    """Render *result* as a human-readable text report.

    :param result: The lint outcome to render.
    :param statistics: When true, append a per-code count summary after the
        violation lines (see :func:`_format_statistics_text`). A clean run has
        no violations to summarise, so the summary is omitted entirely and only
        the "no violations found" line is returned.
    :returns: The rendered text report.
    """
    if result.ok:
        return _append_fixed_text(f"Checked {result.files_checked} file(s); no violations found.", result)
    body = "\n".join(
        f"{violation.filename}:{violation.lineno}:{violation.col_offset}: {violation.code} {violation.message}"
        for violation in _sorted_violations(result.violations)
    )
    if statistics:
        body = f"{body}\n\n{_format_statistics_text(result)}"
    return _append_fixed_text(body, result)


def _append_fixed_text(report: str, result: LintResult) -> str:
    """Append the "fixed" baseline-entry count line to *report* when any exist.

    A baselined violation that matched nothing this run is "fixed" and can be
    pruned. This is informational only -- it never changes the exit code (the run
    stays clean) -- so the line is appended and omitted entirely when there is
    nothing to report, keeping baseline-free output byte-identical.
    """
    count = len(result.baseline_fixed)
    if count == 0:
        return report
    if count == 1:
        note = "1 baseline entry no longer matches anything (fixed); remove it from the baseline."
    else:
        note = f"{count} baseline entries no longer match anything (fixed); remove them from the baseline."
    return f"{report}\n\n{note}"


def format_json(result: LintResult, *, statistics: bool = False) -> str:
    """Render *result* as a deterministic, indented JSON document.

    The document carries a ``schema_version`` integer so consumers can detect
    incompatible shape changes; existing keys stay unchanged across additive
    revisions.

    :param result: The lint outcome to render.
    :param statistics: When true, add an additive ``statistics`` object mapping
        each violated code to ``{"count", "description"}``. The key appears only
        when requested and leaves every existing key untouched, so
        ``schema_version`` stays ``1`` (an additive optional key cannot break a
        consumer that ignores unknown keys); an empty object is emitted when
        there are no violations.

        A separate additive ``baseline`` object (``{"fixed": [...]}``) is emitted
        only when a baseline was applied and left stale ("fixed") entries; it too
        keeps ``schema_version`` at ``1`` and is absent otherwise. The
        ``github``/``sarif`` formats omit fixed entries entirely.
    :returns: The rendered JSON document.
    """
    payload: dict[str, object] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "ok": result.ok,
        "files_checked": result.files_checked,
        "violations": [violation.__dict__ for violation in _sorted_violations(result.violations)],
    }
    if statistics:
        payload["statistics"] = {
            code: {"count": count, "description": description} for code, count, description in _statistics_rows(result)
        }
    if result.baseline_fixed:
        payload["baseline"] = {
            "fixed": [
                {"fingerprint": entry.fingerprint, "occurrence": entry.occurrence} for entry in result.baseline_fixed
            ]
        }
    return json.dumps(payload, indent=2, sort_keys=True)


def _statistics_rows(result: LintResult) -> list[tuple[str, int, str]]:
    """Return ``(code, count, description)`` rows for *result*, sorted by code.

    Counts are per violated rule code; descriptions come from
    :attr:`LintResult.registered_rules` (empty string when a code is absent,
    e.g. a hand-built result). Codes with no violations are not listed.
    """
    counts = Counter(violation.code for violation in result.violations)
    descriptions = dict(result.registered_rules)
    return [(code, counts[code], descriptions.get(code, "")) for code in sorted(counts)]


def _format_statistics_text(result: LintResult) -> str:
    """Render the per-code count summary block, e.g. ``X001  3  Bare except``.

    Columns are the rule code left-justified to the widest code, the count
    right-justified to the widest count, and the description, each separated by
    two spaces. Trailing whitespace (an empty description) is stripped.
    """
    rows = _statistics_rows(result)
    code_width = max(len(code) for code, _, _ in rows)
    count_width = max(len(str(count)) for _, count, _ in rows)
    return "\n".join(
        f"{code:<{code_width}}  {count:>{count_width}}  {description}".rstrip() for code, count, description in rows
    )


JSON_SCHEMA_VERSION = 1
"""Schema version stamped into :func:`format_json` output (bumped on breaking changes)."""

SARIF_VERSION = "2.1.0"
"""SARIF specification version emitted by :func:`format_sarif`."""

SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
"""``$schema`` URI advertised by :func:`format_sarif` output."""


def _escape_github_data(value: str) -> str:
    """Escape a GitHub workflow-command *message* payload.

    Applies the data-string rules from the GitHub Actions toolkit: ``%`` is
    escaped first (so subsequently introduced ``%`` sequences are not
    double-escaped), then carriage return and newline.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_github_property(value: str) -> str:
    """Escape a GitHub workflow-command *property* value.

    Property values carry the data-string escapes plus ``:`` and ``,``, which
    otherwise terminate a property or the property list.
    """
    return _escape_github_data(value).replace(":", "%3A").replace(",", "%2C")


def format_github(result: LintResult) -> str:
    """Render *result* as GitHub Actions ``::error`` workflow commands.

    Emits one annotation per violation, ordered by :func:`_sorted_violations`
    (plan contract C9). Columns are 1-based (the engine stores a 0-based
    ``col_offset``). An empty result renders as the empty string, so the CLI
    prints nothing for a clean run rather than a stray annotation.

    :param result: The lint outcome to render.
    :returns: Newline-separated workflow commands, or ``""`` when clean.
    """
    lines = [
        f"::error file={_escape_github_property(violation.filename)},"
        f"line={violation.lineno},col={violation.col_offset + 1},"
        f"title={_escape_github_property(violation.code)}::"
        f"{_escape_github_data(violation.message)}"
        for violation in _sorted_violations(result.violations)
    ]
    return "\n".join(lines)


def _sarif_uri(filename: str) -> str:
    """Return *filename* as a forward-slash SARIF ``artifactLocation`` URI.

    :func:`lint_paths` display names are already ``base_dir``-relative POSIX
    paths (plan contract C6); the backslash replacement only matters for the
    absolute-path fallback on Windows, keeping the URI portable.
    """
    return filename.replace("\\", "/")


def format_sarif(result: LintResult) -> str:
    """Render *result* as a SARIF 2.1.0 document (built with ``json`` only, C8).

    The single run advertises the ``flakeforge`` driver and its package version,
    lists one ``rules[]`` entry per registered code (from
    :attr:`LintResult.registered_rules`), and one ``results[]`` entry per
    violation with a 1-based region. Output is deterministic: results follow the
    C9 ordering and rules keep the registry's code order, with ``sort_keys``
    stabilising object-key order.

    :param result: The lint outcome to render.
    :returns: An indented SARIF 2.1.0 JSON document.
    """
    # Imported lazily to avoid an import cycle: the package ``__init__`` imports
    # this module, so ``__version__`` is not yet bound at api import time.
    from . import __version__  # noqa: X006

    driver_rules = [
        {"id": code, "shortDescription": {"text": description}} for code, description in result.registered_rules
    ]
    results = [
        {
            "ruleId": violation.code,
            "level": "error",
            "message": {"text": violation.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": _sarif_uri(violation.filename)},
                        "region": {
                            "startLine": violation.lineno,
                            "startColumn": violation.col_offset + 1,
                        },
                    }
                }
            ],
        }
        for violation in _sorted_violations(result.violations)
    ]
    document = {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "flakeforge",
                        "version": __version__,
                        "rules": driver_rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(document, indent=2, sort_keys=True)


FORMATTERS: dict[str, Callable[[LintResult], str]] = {
    "text": format_text,
    "json": format_json,
    "github": format_github,
    "sarif": format_sarif,
}
"""Single source of truth mapping each output-format name to its renderer.

The CLI ``--output-format`` choices and :func:`validate_output_format` both read
this table, so registering a formatter here is enough to expose it everywhere
(04.0 extends it with CI-native formats).
"""

KNOWN_OUTPUT_FORMATS: tuple[str, ...] = tuple(FORMATTERS)
"""Registered output-format names, derived from :data:`FORMATTERS` (kept for API stability)."""

STATISTICS_FORMATS: tuple[str, ...] = ("text", "json")
"""Formats that render a ``--statistics`` summary; ``github``/``sarif`` ignore it.

:func:`format_result` only threads ``statistics`` into these formatters; the CLI
consults this table to warn (on stderr) when ``--statistics`` is set for a format
that drops it. The engine never prints -- surfacing the warning is the CLI's job.
"""


def validate_output_format(output_format: str) -> str:
    """Return *output_format* if it names a known formatter.

    :param output_format: The requested formatter name.
    :returns: The validated formatter name, unchanged.
    :raises ValueError: If *output_format* is not a registered formatter; the
        CLI maps this to exit code ``2`` (plan contract C1).
    """
    if output_format not in FORMATTERS:
        known = ", ".join(KNOWN_OUTPUT_FORMATS)
        raise ValueError(f"Unknown output format {output_format!r}; choose from {known}")
    return output_format


def format_result(result: LintResult, output_format: str, *, statistics: bool = False) -> str:
    """Render *result* using the formatter named by *output_format*.

    ``statistics`` is a rendering concern threaded here rather than through the
    :data:`FORMATTERS` registry signature, which stays ``Callable[[LintResult],
    str]``. It is applied only for the formats in :data:`STATISTICS_FORMATS`
    (``text``/``json``); ``github``/``sarif`` silently ignore it (the CLI emits
    the user-facing warning).

    :param result: The lint outcome to render.
    :param output_format: Name of a registered formatter.
    :param statistics: When true, request a per-code count summary from formats
        that support one.
    :returns: The rendered report text.
    :raises ValueError: If *output_format* is not a registered formatter.
    """
    validated = validate_output_format(output_format)
    formatter = FORMATTERS[validated]
    if statistics and validated in STATISTICS_FORMATS:
        return formatter(result, statistics=True)  # type: ignore[call-arg]
    return formatter(result)


def _is_rule_enabled(code: str, config: LintConfig) -> bool:
    """Return whether *code* survives the config's select/ignore filters."""
    if config.select and not _matches_code_prefix(code, config.select):
        return False
    if _matches_code_prefix(code, config.ignore):
        return False
    return True


def _is_per_file_ignored(violation: RuleViolation, config: LintConfig) -> bool:
    """Return whether *violation* is silenced by a ``per_file_ignores`` entry.

    Suppression is engine-owned (repo convention): rules never see per-file
    ignores. A violation is dropped when its file matches an entry's glob and its
    code starts with one of that entry's prefixes. Globs are matched
    base-relative through :func:`discovery.path_matches_any` with the config's
    ``base_dir`` (plan contract C6), the same anchoring ``include``/``exclude``
    and the ``# noqa`` path policy use, so the result is independent of the
    process cwd. This runs *after* rule execution and *before* ``# noqa``
    handling.

    X015 seam (unused-``# noqa``, 05.0/03): because this predicate fires before
    :func:`_is_noqa_suppressed`, a per-file-ignored violation never reaches the
    noqa check, so a ``# noqa`` on that line would look unused. A future X015 pass
    must therefore treat a per-file-ignored violation as still *covering* any
    ``# noqa`` on its line -- i.e. compute "unused noqa" against the union of
    noqa-suppressed and per-file-ignored violations, not just the former.
    Keeping this as a separate, side-effect-free predicate (rather than folding
    the code prefixes into the noqa path) preserves that seam without building
    X015 now.

    :param violation: The candidate violation.
    :param config: The validated effective configuration.
    :returns: ``True`` when a matching entry silences *violation*.
    """
    if not config.per_file_ignores:
        return False
    root_dir = config.base_dir or Path.cwd()
    for glob, codes in config.per_file_ignores:
        if not codes:
            continue
        if path_matches_any(violation.filename, (glob,), root_dir) and _matches_code_prefix(violation.code, codes):
            return True
    return False


def _is_noqa_suppressed(
    violation: RuleViolation,
    source: Optional[str],
    config: LintConfig,
    *,
    apply_noqa: bool,
) -> bool:
    """Return whether *violation* is suppressed by a ``# noqa`` on its line."""
    if not apply_noqa or not config.allow_noqa or source is None:
        return False
    if not _path_allows_noqa(violation.filename, config):
        return False

    lines = source.splitlines()
    index = violation.lineno - 1
    if index < 0 or index >= len(lines):
        return False

    codes = _parse_noqa_codes(lines[index])
    if codes is None:
        return False
    if not codes:
        return True
    return _matches_code_prefix(violation.code, tuple(codes))


def _path_allows_noqa(filename: str, config: LintConfig) -> bool:
    """Return whether ``# noqa`` is permitted for *filename* under *config*."""
    root_dir = config.base_dir or Path.cwd()
    if path_matches_any(filename, config.noqa_forbidden, root_dir):
        return False
    if config.noqa_allowed and not path_matches_any(filename, config.noqa_allowed, root_dir):
        return False
    return True


def _parse_noqa_codes(line: str) -> Optional[frozenset[str]]:
    """Parse the codes from a ``# noqa`` comment on *line*.

    :returns: ``None`` when the line carries no ``noqa`` comment, an empty set
        for a bare ``# noqa`` (suppress everything), or the specific codes.
    """
    comment = _extract_comment(line)
    if comment is None:
        return None
    return _noqa_codes_from_comment(comment)


def _noqa_codes_from_comment(comment: str) -> Optional[frozenset[str]]:
    """Parse the ``noqa`` codes out of an already-extracted *comment* body.

    *comment* is the trailing comment text with its leading ``#`` and
    surrounding whitespace removed (see :func:`_extract_comment`).

    :returns: ``None`` when the comment is not a ``noqa`` directive, an empty set
        for a bare ``# noqa`` (suppress everything), or the specific codes.
    """
    lowered = comment.lower()
    if not lowered.startswith("noqa"):
        return None
    if ":" not in comment:
        return frozenset()

    _, raw_codes = comment.split(":", 1)
    cleaned_codes: list[str] = []
    for chunk in raw_codes.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        cleaned_codes.append(cleaned.split()[0].upper())
    return frozenset(cleaned_codes)


def _iter_noqa_directives(source: str) -> Iterable[tuple[int, int, frozenset[str]]]:
    """Yield ``(lineno, col_offset, codes)`` for every ``# noqa`` in *source*.

    *lineno* is 1-based and *col_offset* is the 0-based column of the ``#`` that
    opens the comment (the X015 report position). *codes* is the parsed
    directive: an empty frozenset for a bare ``# noqa``, otherwise the named
    codes. Tokenising is done over the whole module so ``#`` inside string
    literals never registers as a comment.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        # A module that cannot be fully tokenised (e.g. an unterminated string)
        # contributes no directives; the AST was already parsed by the caller,
        # so reaching here is defensive.
        tokens = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        comment = token.string.removeprefix("#").strip()
        codes = _noqa_codes_from_comment(comment)
        if codes is None:
            continue
        yield token.start[0], token.start[1], codes


def _unused_noqa_violations(
    context: RuleContext,
    config: LintConfig,
    registry: RuleRegistry,
    covered_by_line: dict[int, set[str]],
    *,
    apply_noqa: bool,
) -> list[RuleViolation]:
    """Emit X015 for every ``# noqa`` directive that suppressed nothing.

    Suppression and ``# noqa`` handling are engine-owned (repo convention), so
    X015 is emitted here rather than by an AST-walking rule. A directive is
    unused when:

    * it is bare (``# noqa``) and nothing on its line was covered -- neither
      suppressed by that ``# noqa`` nor dropped by ``per_file_ignores``; or
    * it is coded (``# noqa: CODE, ...``) and at least one listed entry is unused
      -- an *unregistered* code (always unused) or a registered, currently
      *enabled* code that matched nothing covered on its line. A coded entry that
      is registered but disabled by ``select`` / ``ignore`` is skipped, never
      reported, because it may be honoured by another run (e.g. a CI ``--select``
      subset). This is the documented subset caveat: a *bare* ``# noqa`` names no
      codes, so it cannot be exempted this way and may read as unused under a
      narrow ``select``.

    X015 is not itself suppressible by ``# noqa`` (that would invite a
    self-reference loop), but it still honours ``select`` / ``ignore`` and
    ``per_file_ignores`` here (baseline runs later, in :func:`lint_paths`). It is
    skipped entirely whenever ``# noqa`` is inert for this file -- ``apply_noqa``
    off, ``allow_noqa`` false, absent source, or a ``noqa_allowed`` /
    ``noqa_forbidden`` path policy that forbids ``# noqa`` -- since a directive
    that is ignored by policy cannot be called unused.

    :param context: The module analysis context.
    :param config: The validated effective configuration.
    :param registry: The resolved registry, for the set of known codes.
    :param covered_by_line: Codes covered per source line during rule execution.
    :param apply_noqa: Whether ``# noqa`` handling is active for this run.
    :returns: The X015 violations to report, in source order.
    """
    if not apply_noqa or not config.allow_noqa or context.source is None:
        return []
    if not _path_allows_noqa(context.filename, config):
        return []
    if not _is_rule_enabled(UNUSED_NOQA_CODE, config):
        return []
    # The registry is the source of truth for which rules exist: an explicit
    # registry without X015 (e.g. ``check_source(rules=[...])``) emits none.
    if not any(registration.code == UNUSED_NOQA_CODE for registration in registry.enabled_rules()):
        return []

    known_codes = registry.known_codes()
    results: list[RuleViolation] = []
    for lineno, col_offset, codes in _iter_noqa_directives(context.source):
        covered = covered_by_line.get(lineno, set())
        if not _noqa_directive_is_unused(codes, covered, config, known_codes):
            continue
        violation = RuleViolation(
            filename=context.filename,
            lineno=lineno,
            col_offset=col_offset,
            code=UNUSED_NOQA_CODE,
            message=UNUSED_NOQA_MESSAGE,
        )
        if _is_per_file_ignored(violation, config):
            continue
        results.append(violation)
    return results


def _noqa_directive_is_unused(
    codes: frozenset[str],
    covered: set[str],
    config: LintConfig,
    known_codes: Sequence[str],
) -> bool:
    """Return whether the parsed ``# noqa`` *codes* suppressed nothing usable.

    See :func:`_unused_noqa_violations` for the full contract. A bare directive
    (empty *codes*) is unused when nothing on its line was covered. A coded
    directive is unused when any listed entry is unregistered, or is registered
    and currently enabled yet matched nothing in *covered*.
    """
    if not codes:
        return not covered
    for code in codes:
        matched_known = tuple(known for known in known_codes if known.startswith(code))
        if not matched_known:
            # Unknown / unregistered code: it can never suppress anything.
            return True
        if not any(_is_rule_enabled(known, config) for known in matched_known):
            # Registered but disabled this run; another run may still use it.
            continue
        if not any(covered_code.startswith(code) for covered_code in covered):
            return True
    return False


def _extract_comment(line: str) -> Optional[str]:
    """Return the trailing comment text on *line*, or ``None`` if absent."""
    try:
        for token in tokenize.generate_tokens(io.StringIO(line).readline):
            if token.type == tokenize.COMMENT:
                return token.string.removeprefix("#").strip()
    except tokenize.TokenError:
        # A single line that cannot be tokenised in isolation (e.g. an
        # unterminated string) exposes no recognisable comment.
        no_comment: Optional[str] = None
        return no_comment
    return None


def _matches_code_prefix(code: str, prefixes: Sequence[str]) -> bool:
    """Return whether *code* starts with any entry in *prefixes*."""
    return any(code.startswith(prefix) for prefix in prefixes)


def _sorted_violations(
    violations: Sequence[RuleViolation],
) -> list[RuleViolation]:
    """Return *violations* sorted by location, code, and message."""
    return sorted(
        violations,
        key=lambda violation: (
            violation.filename,
            violation.lineno,
            violation.col_offset,
            violation.code,
            violation.message,
        ),
    )


def _resolve_target_paths(
    paths: Optional[Sequence[Union[str, Path]]],
    config: LintConfig,
) -> tuple[Path, ...]:
    """Resolve the concrete directories/files to scan for a lint run.

    Explicit *paths* win; otherwise the config's ``include`` roots are used, and
    finally ``src``/``tests`` (or the base directory) as defaults.
    """
    root_dir = (config.base_dir or Path.cwd()).resolve()
    if paths:
        resolved: list[Path] = []
        for raw_path in paths:
            candidate = Path(raw_path)
            resolved.append(candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve())
        return tuple(resolved)
    if config.include:
        return _include_traversal_roots(config.include, root_dir) or (root_dir,)
    defaults = tuple(candidate for candidate in (root_dir / "src", root_dir / "tests") if candidate.exists())
    return defaults or (root_dir,)


def _include_traversal_roots(include: Sequence[str], root_dir: Path) -> tuple[Path, ...]:
    """Derive existing traversal roots from the config's include patterns."""
    roots: list[Path] = []
    for pattern in include:
        if not pattern.strip():
            continue
        root = _safe_traversal_root(pattern, root_dir)
        if root.exists() and root not in roots:
            roots.append(root)
    return tuple(roots)


def _safe_traversal_root(pattern: str, root_dir: Path) -> Path:
    """Return the fixed directory prefix of *pattern* before any glob wildcard."""
    parts = Path(pattern).parts
    root = Path(parts[0]) if parts and Path(parts[0]).is_absolute() else root_dir
    prefix: list[str] = []
    for part in parts:
        if any(character in part for character in "*?["):
            break
        prefix.append(part)
    if not prefix:
        return root
    if Path(prefix[0]).is_absolute():
        return Path(*prefix).resolve()
    return (root_dir / Path(*prefix)).resolve()


def _display_filename(path: Path, root_dir: Path) -> str:
    """Return *path* relative to the config ``base_dir``, else the cwd, else raw.

    The config ``base_dir`` (*root_dir*) is tried first (plan contract C6) so a
    ``--config`` run prints identical, base-relative names whatever the process
    cwd is -- this fixes G7, where an ancestor cwd previously produced a long
    ``project/pkg/m.py`` display instead of ``pkg/m.py``. The cwd is a secondary
    anchor so sibling ``include`` roots such as ``../src`` still render cleanly,
    and the raw (absolute) path is the last-resort fallback for a file that lies
    under neither anchor.
    """
    resolved = path.resolve()
    for base in (root_dir, Path.cwd().resolve()):
        if resolved.is_relative_to(base):
            return resolved.relative_to(base).as_posix()
    return str(path)
