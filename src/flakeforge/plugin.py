"""Thin Flake8 adapter for built-in flakeforge rules.

Task 0001 keeps installed external rule-provider discovery authoritative in the
standalone engine and CLI. The Flake8 adapter intentionally caches only the
built-in registry in this scaffold.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .api import check_tree
from .config import LintConfig
from .registry import resolve_registry


class ProjectRulesPlugin:
    """Flake8 plugin entry point exposing the built-in flakeforge rules.

    It bypasses ``lint_paths`` and therefore emits no fault-telemetry events in v1.

    :cvar name: Plugin name reported to Flake8.
    :cvar version: Plugin version, mirrored from the package version.
    :cvar _config: Lint configuration derived from Flake8 options.
    :cvar _registry: Cached registry of built-in rules (entry points excluded).
    """

    name = "flakeforge"
    version = __version__
    _config = LintConfig()
    _registry = resolve_registry(include_entry_points=False)

    @classmethod
    def add_options(cls, option_manager):
        """Register flakeforge specific options on the Flake8 option manager."""
        option_manager.add_option(
            "--flakeforge-no-noqa",
            action="store_true",
            parse_from_config=True,
            default=False,
            help="Disable noqa suppression inside the flakeforge adapter.",
        )

    @classmethod
    def parse_options(cls, options):
        """Build the cached lint configuration from parsed Flake8 options."""
        select = _normalize_code_prefixes(
            getattr(options, "select", ()),
            getattr(options, "extend_select", ()),
        )
        ignore = _normalize_code_prefixes(
            getattr(options, "ignore", ()),
            getattr(options, "extend_ignore", ()),
        )
        disable_noqa = bool(getattr(options, "disable_noqa", False) or getattr(options, "flakeforge_no_noqa", False))
        cls._config = LintConfig(
            select=select,
            ignore=ignore,
            allow_noqa=not disable_noqa,
        )

    def __init__(self, tree, filename: str = "<unknown>"):
        """Store the parsed AST and filename for a single Flake8 check pass."""
        self.tree = tree
        self.filename = filename

    def run(self):
        """Yield Flake8-style violation tuples for the stored module."""
        source = None
        if self.filename not in (None, "-", "stdin", "<unknown>"):
            try:
                source = Path(self.filename).read_text(encoding="utf-8")
            except OSError:
                source = None

        for violation in check_tree(
            self.tree,
            self.filename,
            source,
            validate_selectors=False,
            config=type(self)._config,
            registry=type(self)._registry,
            # Flake8 owns noqa handling in the adapter, so the engine must not
            # emit the unused-noqa code (X015) here; its view of what a directive
            # suppresses can differ from Flake8's.
            emit_unused_noqa=False,
        ):
            yield (
                violation.lineno,
                violation.col_offset,
                f"{violation.code} {violation.message}",
                type(self),
            )


def _normalize_code_prefixes(*groups) -> tuple[str, ...]:
    """Merge option groups into a de-duplicated tuple of upper-cased codes."""
    codes: list[str] = []
    for group in groups:
        for code in group or ():
            upper = str(code).upper()
            if upper not in codes:
                codes.append(upper)
    return tuple(codes)
