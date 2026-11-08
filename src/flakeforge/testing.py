"""Explicit pytest helper APIs for flakeforge."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

from .api import format_text, lint_paths
from .config import LintConfig


def assert_lint_clean(*paths: Union[str, Path], config: Optional[LintConfig] = None):
    """Assert that linting *paths* produces no violations.

    Telemetry is emitted only if *config* sets ``telemetry_sink`` (via
    :func:`flakeforge.lint_paths`); it is off by default.

    :param paths: Files or directories to lint; defaults to the current working
        directory when no paths are supplied.
    :param config: Optional lint configuration to apply during the check.
    :raises AssertionError: If any violation is found, carrying the formatted
        text report as the assertion message.
    """
    selected: Iterable[Union[str, Path]] = paths or (Path.cwd(),)
    result = lint_paths(selected, config=config)
    if not result.ok:
        raise AssertionError(format_text(result))
