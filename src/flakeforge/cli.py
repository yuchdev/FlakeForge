"""Command-line interface for flakeforge."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Optional

from . import __version__
from .api import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_VIOLATIONS,
    FORMATTERS,
    STATISTICS_FORMATS,
    _is_rule_enabled,
    format_result,
    lint_paths,
    validate_output_format,
)
from .baseline import write_baseline
from .config import (
    LintConfig,
    describe_config_source,
    discovery_anchor,
    isolated_config,
    load_config,
    render_config_template,
    resolve_config_origins,
    validate_config,
)
from .registry import resolve_registry

#: Output formats ``config show`` can render itself in. The report is a config
#: dump, so the annotation formats (``github``/``sarif``) have no meaning here
#: and are rejected as an invalid invocation (C1).
_CONFIG_SHOW_FORMATS: tuple[str, ...] = ("text", "json")


def _add_common_arguments(parser: argparse.ArgumentParser):
    """Add the shared config-selection and override options to *parser*.

    These are the arguments every path-anchored subcommand accepts, so ``check``
    and ``config show`` resolve config identically (task 03.0 shared parent
    parser). Defined once here to keep the two subcommands in lockstep.

    :param parser: The (sub)parser or ``add_help=False`` parent to populate.
    """
    parser.add_argument("paths", nargs="*")
    config_source = parser.add_mutually_exclusive_group()
    config_source.add_argument("--config", help="Path to pyproject.toml or flakeforge.toml")
    config_source.add_argument(
        "--no-config",
        action="store_true",
        help="Skip config discovery; use built-in defaults plus CLI flags only",
    )
    parser.add_argument("--select", action="append", default=[])
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument(
        "--per-file-ignores",
        action="append",
        default=[],
        metavar="GLOB:CODE[,CODE]",
        help="Suppress rule-code prefixes for files matching GLOB; repeatable, replaces the config file's table",
    )
    parser.add_argument(
        "--noqa",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Honour (or, with --no-noqa, disable) # noqa suppression",
    )
    parser.add_argument("--rule-module", action="append", default=[])
    parser.add_argument(
        "--rule-plugins",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Load (or, with --no-rule-plugins, skip) installed flakeforge.rules providers",
    )
    parser.add_argument("--output-format", choices=tuple(FORMATTERS), default=None)
    parser.add_argument(
        "--baseline",
        default=None,
        metavar="PATH",
        help="Suppress violations whose fingerprint is recorded in the baseline file at PATH",
    )
    parser.add_argument(
        "--statistics",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Append (or, with --no-statistics, suppress) a per-code count summary; "
            "text and json only, ignored with a warning for github/sarif"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser with its ``check`` and ``config`` commands.

    ``check`` and ``config show`` share one parent parser
    (:func:`_add_common_arguments`) so both accept the same config-selection,
    path-anchor and override flags, and ``config show`` reports exactly what
    ``check`` would use.
    """
    parser = argparse.ArgumentParser(prog="flakeforge")
    parser.add_argument("--version", action="store_true", help="Show package version and exit")

    common = argparse.ArgumentParser(add_help=False)
    _add_common_arguments(common)

    subparsers = parser.add_subparsers(dest="command")
    check_parser = subparsers.add_parser(
        "check",
        parents=[common],
        help="Lint one or more files or directories",
    )
    check_parser.add_argument(
        "--write-baseline",
        default=None,
        metavar="PATH",
        help="Write every current violation's fingerprint to PATH and exit 0 (ignores --baseline)",
    )
    check_parser.add_argument(
        "--telemetry-sink",
        default=None,
        metavar="SPEC",
        help="Emit fault-telemetry events: stdout, logging, file:PATH, sqlite:PATH, or a dotted custom sink",
    )
    check_parser.add_argument(
        "--generated-by-model",
        default=None,
        metavar="ID",
        help="Generator model id recorded on telemetry events",
    )

    config_parser = subparsers.add_parser("config", help="Inspect resolved configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser(
        "show",
        parents=[common],
        help="Print the effective, validated configuration and where each value came from",
    )

    subparsers.add_parser(
        "rules",
        parents=[common],
        help="List every registered rule, its origin, and whether it is enabled",
    )

    # ``init`` deliberately does NOT take the shared parent parser. That parser's
    # flags (--config/--no-config, path anchor, and the override options) are about
    # *reading* the config that a run resolves; ``init`` *writes* a fresh starter
    # config and consults neither discovery nor overrides, so attaching them would
    # advertise behaviour it does not have (reconciliation note for task 03.0's
    # "same parent parser" wording).
    init_parser = subparsers.add_parser(
        "init",
        help="Write a starter flakeforge.toml (or a [tool.flakeforge] table)",
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to write the config into (defaults to the current directory)",
    )
    init_parser.add_argument(
        "--pyproject",
        action="store_true",
        help="Append a [tool.flakeforge] table to DIR/pyproject.toml instead of writing flakeforge.toml",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Run the ``flakeforge`` CLI and return a process exit code.

    :param argv: Optional argument vector; defaults to ``sys.argv`` when omitted.
    :returns: ``EXIT_OK`` when clean, ``EXIT_VIOLATIONS`` when violations are
        found, or ``EXIT_ERROR`` when the invocation fails.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"flakeforge {__version__}")
        return EXIT_OK
    if args.command == "check":
        return _run_check(args)
    if args.command == "config":
        return _run_config(args)
    if args.command == "rules":
        return _run_rules(args)
    if args.command == "init":
        return _run_init(args)
    parser.print_help()
    return EXIT_ERROR


def _run_check(args: argparse.Namespace) -> int:
    """Execute the ``check`` subcommand and return its exit code."""
    missing = _missing_paths(args.paths)
    if missing:
        return EXIT_ERROR

    writing_baseline = args.write_baseline is not None
    try:
        config = _build_cli_config(args)
        if writing_baseline:
            # --write-baseline records the full current set, so an existing
            # baseline is not applied first (it would hide violations from the
            # file being written).
            config = config.merge(baseline="")
        validate_output_format(config.output_format)
        registry = resolve_registry(
            rule_modules=config.rule_modules,
            include_entry_points=config.rule_plugins,
            project_root=config.rule_module_root,
        )
        config = validate_config(config, registry.known_codes())
        _emit_warnings(config)
        _warn_statistics_ignored(config)
        result = lint_paths(
            tuple(Path(path) for path in args.paths),
            config=config,
            registry=registry,
            capture_run=writing_baseline,
        )
        if writing_baseline:
            return _write_baseline_result(result, args.write_baseline)
        output = format_result(result, config.output_format, statistics=config.statistics)
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        print(f"flakeforge: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if output:
        print(output)
    return EXIT_OK if result.ok else EXIT_VIOLATIONS


def _write_baseline_result(result, path: str) -> int:
    """Persist every current violation's fingerprint and exit ``0`` (C1).

    Writes the full reported set (after select/ignore, ``per_file_ignores`` and
    ``# noqa``), regardless of how many violations there are, so the run always
    exits clean; the baseline is the record of what is being accepted today.
    """
    written = write_baseline(path, result.fingerprints)
    count = len(result.fingerprints)
    noun = "entry" if count == 1 else "entries"
    print(f"Wrote {count} baseline {noun} to {written}")
    return EXIT_OK


def _run_config(args: argparse.Namespace) -> int:
    """Dispatch the ``config`` command group to its subcommands.

    A bare ``flakeforge config`` is an invalid invocation (exit ``2``, C1), so it
    reports on stderr instead of going through argparse's ``--help`` exit ``0``.
    """
    if args.config_command == "show":
        return _config_show(args)
    print("flakeforge: config requires a subcommand: show", file=sys.stderr)
    return EXIT_ERROR


def _run_rules(args: argparse.Namespace) -> int:
    """List every registered rule in the resolved registry.

    Resolves config and the rule registry exactly as ``check`` does -- same
    discovery, same ``resolve_registry`` (including ``rule_plugins`` and the
    project-root import window), same ``validate_config`` and stderr warnings --
    so the catalogue it reports is what ``check`` would run. Each row carries the
    rule's code, short description, provider origin (``builtin``,
    ``rule_module:<name>``, or ``entry_point:<dist>``) and whether it is enabled
    under the effective ``select``/``ignore``. Rows are ordered by code. The
    report renders only as ``text`` or ``json``; the annotation formats are an
    invalid invocation (C1). A successful listing exits ``0``; invalid config
    exits ``2``.
    """
    if _missing_paths(args.paths):
        return EXIT_ERROR

    report_format = args.output_format or "text"
    rejected = _reject_annotation_report_format(report_format, "rules")
    if rejected is not None:
        return rejected

    try:
        config = _build_cli_config(args)
        validate_output_format(config.output_format)
        registry = resolve_registry(
            rule_modules=config.rule_modules,
            include_entry_points=config.rule_plugins,
            project_root=config.rule_module_root,
        )
        config = validate_config(config, registry.known_codes())
        _emit_warnings(config)
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        print(f"flakeforge: {exc}", file=sys.stderr)
        return EXIT_ERROR

    rows = _rule_rows(registry, config)
    if report_format == "json":
        print(_render_rules_json(rows))
    else:
        print(_render_rules_text(rows))
    return EXIT_OK


def _run_init(args: argparse.Namespace) -> int:
    """Write a starter config, refusing to overwrite, duplicate, or shadow.

    Two surfaces: ``flakeforge init DIR`` writes ``DIR/flakeforge.toml`` (every
    schema key at its default, one comment per key), while ``--pyproject`` appends
    a ``[tool.flakeforge]`` table to ``DIR/pyproject.toml`` instead. Both render
    from :func:`flakeforge.config.render_config_template` -- no TOML writer (C8).

    Refusals all exit ``2`` (C1) with a ``flakeforge:`` stderr message and touch no
    file: a non-existent *DIR*; an existing target ``flakeforge.toml``; a
    ``pyproject.toml`` that already carries ``[tool.flakeforge]`` or the legacy
    ``[tool.flake8_lint]`` (or cannot be parsed, in ``--pyproject`` mode); and the
    shadowing case from task 02.0 (plan contract C3), where a same-directory
    ``flakeforge.toml`` outranks a ``pyproject.toml`` section -- so we refuse to
    create either surface when the other is already present rather than write a
    file that would be silently ignored. ``--force`` is out of scope. On success
    the written path is printed to stdout and the command exits ``0``.

    :param args: Parsed ``init`` arguments (``directory`` and ``pyproject``).
    :returns: :data:`EXIT_OK` on a successful write, else :data:`EXIT_ERROR`.
    """
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"flakeforge: not a directory: {directory}", file=sys.stderr)
        return EXIT_ERROR
    if args.pyproject:
        return _init_pyproject(directory)
    return _init_flat(directory)


def _init_flat(directory: Path) -> int:
    """Write ``directory/flakeforge.toml``, refusing to overwrite or shadow."""
    target = directory / "flakeforge.toml"
    if target.exists():
        print(f"flakeforge: {target} already exists", file=sys.stderr)
        return EXIT_ERROR

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            parsed = tomllib.loads(pyproject.read_bytes().decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            # An unparseable sibling can never be loaded, so it cannot be
            # shadowed; the new flakeforge.toml stands.
            parsed = {}
        shadowed = _flakeforge_section_in(parsed)
        if shadowed is not None:
            print(
                f"flakeforge: {pyproject} already defines {shadowed}; a new flakeforge.toml would shadow it",
                file=sys.stderr,
            )
            return EXIT_ERROR

    if not _create_exclusively(target, render_config_template(pyproject=False).encode("utf-8")):
        return EXIT_ERROR
    print(f"Wrote {target}")
    return EXIT_OK


def _init_pyproject(directory: Path) -> int:
    """Append a ``[tool.flakeforge]`` table to ``directory/pyproject.toml``.

    Creates ``pyproject.toml`` when it is absent; otherwise preserves the existing
    bytes verbatim and appends after a separating blank line. Refuses (exit ``2``)
    when the file is unparseable or already defines a flakeforge section, or when a
    same-directory ``flakeforge.toml`` would shadow the new table (C3).
    """
    flat = directory / "flakeforge.toml"
    if flat.exists():
        print(
            f"flakeforge: {flat} already exists and would shadow a new [tool.flakeforge] table in pyproject.toml",
            file=sys.stderr,
        )
        return EXIT_ERROR

    pyproject = directory / "pyproject.toml"
    if pyproject.exists():
        raw = pyproject.read_bytes()
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            print(
                f"flakeforge: {pyproject} is not valid TOML; refusing to append",
                file=sys.stderr,
            )
            return EXIT_ERROR
        existing = _flakeforge_section_in(parsed)
        if existing is not None:
            print(f"flakeforge: {pyproject} already defines {existing}", file=sys.stderr)
            return EXIT_ERROR
    else:
        raw = b""

    block = render_config_template(pyproject=True).encode("utf-8")
    if raw:
        separator = b"" if raw.endswith(b"\n") else b"\n"
        # Append rather than rewrite, so the existing bytes are never re-written.
        with pyproject.open("ab") as handle:
            handle.write(separator + b"\n" + block)
    elif not _create_exclusively(pyproject, block):
        return EXIT_ERROR
    print(f"Wrote {pyproject}")
    return EXIT_OK


def _create_exclusively(target: Path, content: bytes) -> bool:
    """Create *target* with *content*, refusing any existing entry.

    Exclusive creation (``O_EXCL``) also fails on a dangling symlink, so a link
    planted between the existence check and the write cannot redirect it.

    :returns: ``True`` when written; ``False`` after reporting the refusal.
    """
    try:
        with target.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        print(f"flakeforge: {target} already exists", file=sys.stderr)
        return False
    return True


def _flakeforge_section_in(parsed: dict[str, object]) -> Optional[str]:
    """Name the flakeforge ``[tool]`` section a parsed pyproject already defines.

    :param parsed: A parsed ``pyproject.toml`` mapping.
    :returns: ``"[tool.flakeforge]"`` or the legacy ``"[tool.flake8_lint]"`` when
        that section is present (canonical takes precedence), else ``None``.
    """
    tool = parsed.get("tool")
    if not isinstance(tool, dict):
        return None
    if "flakeforge" in tool:
        return "[tool.flakeforge]"
    if "flake8_lint" in tool:
        return "[tool.flake8_lint]"
    return None


def _config_show(args: argparse.Namespace) -> int:
    """Report the effective, validated config and the origin of each value.

    Resolves config exactly as ``check`` does -- same discovery, same
    ``validate_config`` against the resolved registry, same stderr warnings --
    so what it prints is what ``check`` would act on. Invalid config exits ``2``
    (C1). The report itself renders only as ``text`` or ``json``; the annotation
    formats are rejected. ``config.py`` never prints, so all rendering lives here.
    """
    if _missing_paths(args.paths):
        return EXIT_ERROR

    report_format = args.output_format or "text"
    rejected = _reject_annotation_report_format(report_format, "config show")
    if rejected is not None:
        return rejected

    try:
        file_config = _load_base_config(args)
        config = _build_cli_config(args)
        validate_output_format(config.output_format)
        registry = resolve_registry(
            rule_modules=config.rule_modules,
            include_entry_points=config.rule_plugins,
            project_root=config.rule_module_root,
        )
        config = validate_config(config, registry.known_codes())
        _emit_warnings(config)
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        print(f"flakeforge: {exc}", file=sys.stderr)
        return EXIT_ERROR

    origins = resolve_config_origins(file_config, _cli_override_keys(args))
    anchor = discovery_anchor(args.paths, cwd=Path.cwd())
    source_file, section = describe_config_source(config)
    if report_format == "json":
        print(_render_config_show_json(config, origins, anchor, source_file, section))
    else:
        print(_render_config_show_text(config, origins, anchor, source_file, section))
    return EXIT_OK


def _missing_paths(paths: list[str]) -> bool:
    """Report each non-existent path in *paths* to stderr; return whether any."""
    missing = [path for path in paths if not Path(path).exists()]
    for path in missing:
        print(f"flakeforge: path does not exist: {path}", file=sys.stderr)
    return bool(missing)


def _reject_annotation_report_format(report_format: str, command: str) -> Optional[int]:
    """Reject annotation output formats for a text/json-only report subcommand.

    The introspection subcommands (``config show``, ``rules``) print a report,
    not violations, so the annotation formats (``github``/``sarif``) have no
    meaning and are an invalid invocation (C1). Shared by both so they reject
    identically.

    :param report_format: The resolved report format (CLI flag or ``"text"``).
    :param command: The subcommand label used in the error message.
    :returns: :data:`EXIT_ERROR` when *report_format* is not a report format
        (after printing to stderr), or ``None`` when it is acceptable.
    """
    if report_format in _CONFIG_SHOW_FORMATS:
        return None
    print(
        f"flakeforge: {command} supports only text and json output, not {report_format!r}",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _load_base_config(args: argparse.Namespace) -> LintConfig:
    """Load the on-disk config selected by ``--config``/``--no-config``/discovery.

    This is the pre-override configuration (before CLI flags are merged in),
    shared by ``check`` and ``config show`` so both select the same file.
    """
    if args.config:
        return load_config(args.config, cwd=Path.cwd())
    if args.no_config:
        return isolated_config(args.paths, cwd=Path.cwd())
    anchor = discovery_anchor(args.paths, cwd=Path.cwd())
    return load_config(cwd=anchor)


def _cli_override_keys(args: argparse.Namespace) -> set[str]:
    """Return the schema keys the CLI explicitly overrode on this invocation.

    Used for origin attribution (``config show``): a key is present only when the
    matching flag was actually supplied, distinguishing an explicit ``cli`` value
    from an untouched ``file``/``default`` one.
    """
    keys: set[str] = set()
    if args.select:
        keys.add("select")
    if args.ignore:
        keys.add("ignore")
    if args.include:
        keys.add("include")
    if args.exclude:
        keys.add("exclude")
    if args.per_file_ignores:
        keys.add("per_file_ignores")
    if args.noqa is not None:
        keys.add("allow_noqa")
    if args.rule_module:
        keys.add("rule_modules")
    if args.rule_plugins is not None:
        keys.add("rule_plugins")
    if args.output_format is not None:
        keys.add("output_format")
    if args.statistics is not None:
        keys.add("statistics")
    if args.baseline is not None:
        keys.add("baseline")
    if getattr(args, "telemetry_sink", None) is not None:
        keys.add("telemetry_sink")
    if getattr(args, "generated_by_model", None) is not None:
        keys.add("generated_by_model")
    return keys


def _build_cli_config(args: argparse.Namespace) -> LintConfig:
    """Load the on-disk config and overlay CLI overrides onto it."""
    config = _load_base_config(args)
    select = _split_codes(args.select)
    ignore = _split_codes(args.ignore)
    include = _split_patterns(args.include)
    exclude = _split_patterns(args.exclude)
    per_file_ignores = _parse_per_file_ignores(args.per_file_ignores)
    rule_modules = tuple(args.rule_module)
    # A CLI --baseline is resolved to an absolute path up front, so it stays
    # cwd-relative (unlike a config-file value, which is base_dir-relative, C6);
    # None leaves the file's value in place (precedence C2).
    baseline = str(Path(args.baseline).resolve()) if args.baseline else None
    return config.merge(
        include=include if include else None,
        exclude=exclude if exclude else None,
        per_file_ignores=per_file_ignores if per_file_ignores else None,
        select=select if select else None,
        ignore=ignore if ignore else None,
        allow_noqa=args.noqa,
        rule_modules=_merge_unique(config.rule_modules, rule_modules) if rule_modules else None,
        rule_plugins=args.rule_plugins,
        output_format=args.output_format,
        statistics=args.statistics,
        baseline=baseline,
        telemetry_sink=getattr(args, "telemetry_sink", None),
        generated_by_model=getattr(args, "generated_by_model", None),
    )


def _split_codes(values: list[str]) -> tuple[str, ...]:
    """Split comma-separated ``--select``/``--ignore`` values into codes."""
    parsed: list[str] = []
    for value in values:
        parsed.extend(chunk.strip().upper() for chunk in value.split(",") if chunk.strip())
    return tuple(parsed)


def _split_patterns(values: list[str]) -> tuple[str, ...]:
    """Split comma-separated ``--include``/``--exclude`` values into patterns.

    Mirrors :func:`_split_codes` but preserves case, since these are path
    globs rather than rule codes.
    """
    parsed: list[str] = []
    for value in values:
        parsed.extend(chunk.strip() for chunk in value.split(",") if chunk.strip())
    return tuple(parsed)


def _parse_per_file_ignores(values: list[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse repeatable ``--per-file-ignores "GLOB:CODE[,CODE]"`` flags.

    Each value splits on its first ``:`` into a glob and a comma-separated list
    of upper-cased rule-code prefixes. A supplied flag *replaces* the config
    file's table (plan contract C2); this returns the CLI entries in order, and
    the caller passes ``None`` to :meth:`LintConfig.merge` only when no flag was
    given. A value with no ``:``, an empty glob, or no codes is malformed and
    raises, which the CLI maps to exit ``2`` (plan contract C1). Codes are not
    validated against the registry here -- :func:`validate_config` does that.

    :param values: The raw ``--per-file-ignores`` flag values.
    :returns: Ordered ``(glob, codes)`` pairs.
    :raises ValueError: On a malformed value.
    """
    entries: list[tuple[str, tuple[str, ...]]] = []
    for value in values:
        glob, separator, raw_codes = value.partition(":")
        glob = glob.strip()
        codes = tuple(chunk.strip().upper() for chunk in raw_codes.split(",") if chunk.strip())
        if not separator or not glob or not codes:
            raise ValueError(f"malformed --per-file-ignores value {value!r}; expected GLOB:CODE[,CODE]")
        entries.append((glob, codes))
    return tuple(entries)


def _merge_unique(existing: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    """Append *extra* entries to *existing*, preserving order and uniqueness."""
    merged = list(existing)
    for value in extra:
        if value not in merged:
            merged.append(value)
    return tuple(merged)


def _emit_warnings(config: LintConfig):
    """Print any accumulated configuration warnings to stderr."""
    for warning in config.warnings:
        print(f"flakeforge: {warning}", file=sys.stderr)


def _render_config_show_text(
    config: LintConfig,
    origins: dict[str, str],
    anchor: Path,
    source_file: Optional[Path],
    section: Optional[str],
) -> str:
    """Render the ``config show`` report as an aligned three-column table."""
    if source_file is None:
        source = "defaults"
    else:
        source = str(source_file)
        if section:
            source = f"{source} {section}"
    lines = [
        f"source:   {source}",
        f"anchor:   {anchor}",
        f"base_dir: {config.base_dir if config.base_dir is not None else '-'}",
        "",
    ]
    values = {key: _format_setting_value(key, getattr(config, key)) for key in origins}
    name_width = max(len(key) for key in origins)
    value_width = max(len(value) for value in values.values())
    header = f"{'setting'.ljust(name_width)}  {'value'.ljust(value_width)}  origin"
    lines.append(header)
    for key, origin in origins.items():
        lines.append(f"{key.ljust(name_width)}  {values[key].ljust(value_width)}  {origin}")
    return "\n".join(lines)


def _render_config_show_json(
    config: LintConfig,
    origins: dict[str, str],
    anchor: Path,
    source_file: Optional[Path],
    section: Optional[str],
) -> str:
    """Render the ``config show`` report as a stable, sorted JSON document."""
    document = {
        "base_dir": str(config.base_dir) if config.base_dir is not None else None,
        "discovery_anchor": str(anchor),
        "source": {
            "file": str(source_file) if source_file is not None else None,
            "section": section,
        },
        "settings": {
            key: {"origin": origin, "value": _jsonable_setting_value(key, getattr(config, key))}
            for key, origin in origins.items()
        },
    }
    return json.dumps(document, sort_keys=True, indent=2)


def _rule_rows(registry, config: LintConfig) -> list[dict[str, object]]:
    """Return one ordered row per registered rule for the ``rules`` report.

    Rows follow the registry's deterministic code order. Enablement reuses the
    engine's own :func:`flakeforge.api._is_rule_enabled` (no reimplemented prefix
    matching) combined with the registration's default-enabled flag, so it
    matches exactly what ``check`` would run under the same ``select``/``ignore``.

    :param registry: The resolved :class:`flakeforge.registry.RuleRegistry`.
    :param config: The validated effective configuration.
    :returns: A list of ``{code, description, origin, enabled}`` dictionaries.
    """
    return [
        {
            "code": registration.code,
            "description": registration.description,
            "origin": registration.origin,
            "enabled": registration.enabled and _is_rule_enabled(registration.code, config),
        }
        for registration in registry.all()
    ]


def _render_rules_text(rows: list[dict[str, object]]) -> str:
    """Render the ``rules`` listing as an aligned four-column table.

    Columns are code, enabled/disabled state, origin, and description, each
    left-justified to the widest cell and separated by two spaces. An empty
    registry (impossible in practice, built-ins always load) renders as the
    header row alone.
    """
    header = {"code": "code", "state": "state", "origin": "origin", "description": "description"}
    cells = [
        {
            "code": str(row["code"]),
            "state": "enabled" if row["enabled"] else "disabled",
            "origin": str(row["origin"]),
            "description": str(row["description"]),
        }
        for row in rows
    ]
    code_width = max(len(cell["code"]) for cell in (header, *cells))
    state_width = max(len(cell["state"]) for cell in (header, *cells))
    origin_width = max(len(cell["origin"]) for cell in (header, *cells))
    lines = [
        f"{cell['code'].ljust(code_width)}  {cell['state'].ljust(state_width)}  "
        f"{cell['origin'].ljust(origin_width)}  {cell['description']}".rstrip()
        for cell in (header, *cells)
    ]
    return "\n".join(lines)


def _render_rules_json(rows: list[dict[str, object]]) -> str:
    """Render the ``rules`` listing as a stable, sorted JSON document."""
    document = {"rules": rows}
    return json.dumps(document, sort_keys=True, indent=2)


def _format_setting_value(key: str, value: object) -> str:
    """Render a schema value for the aligned text table.

    ``per_file_ignores`` is a nested table rather than a flat list, so it renders
    compactly as ``{glob=[c1,c2],glob2=[c3]}`` (``{}`` when empty) -- no internal
    whitespace, keeping the aligned-column table parseable.
    """
    if key == "per_file_ignores":
        return _format_per_file_ignores_value(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _format_per_file_ignores_value(value: object) -> str:
    """Render the ``per_file_ignores`` table as ``{glob=[codes],...}``."""
    entries = value if isinstance(value, tuple) else ()
    if not entries:
        return "{}"
    rendered = ",".join(f"{glob}=[{','.join(codes)}]" for glob, codes in entries)
    return "{" + rendered + "}"


def _jsonable_setting_value(key: str, value: object) -> object:
    """Convert a schema value into a JSON-serialisable form.

    Tuples become lists; ``per_file_ignores`` becomes a JSON object mapping each
    glob to its list of code prefixes, mirroring the TOML table it comes from.
    """
    if key == "per_file_ignores":
        entries = value if isinstance(value, tuple) else ()
        return {glob: list(codes) for glob, codes in entries}
    if isinstance(value, tuple):
        return list(value)
    return value


def _warn_statistics_ignored(config: LintConfig):
    """Warn (on stderr) when ``--statistics`` has no effect for the chosen format.

    The ``github`` and ``sarif`` formats drop the summary (see
    :data:`flakeforge.api.STATISTICS_FORMATS`); ``config.py`` never prints, so
    the CLI is the one that surfaces this to the user. The exit code is
    unaffected (plan contract C1).
    """
    if config.statistics and config.output_format not in STATISTICS_FORMATS:
        print(
            f"flakeforge: statistics is ignored for the {config.output_format!r} output format",
            file=sys.stderr,
        )
