"""Matplotlib charts for the telemetry statistics document.

Charts consume the mapping produced by :func:`flakeforge.telemetry.compute_statistics`
and carry only codes, counts and display paths -- never source text. Figures are built
with :class:`matplotlib.figure.Figure` directly (no ``pyplot`` global state), which
renders headless through the Agg canvas, so no display or backend setup is needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from matplotlib.figure import Figure

_LOGGER = logging.getLogger(__name__)

CHART_TYPES: tuple[str, ...] = ("rules", "hit-rate", "classes", "outcomes", "models", "top-files")
"""Every chart type; ``--plot-statistics-types`` defaults to all of them."""

_COLOR = "#3b6ea5"
_ACCENT = "#c0504d"
_DPI = 120


def parse_chart_types(raw: Sequence[str]) -> tuple[str, ...]:
    """Validate chart type names; an empty selection means every type.

    :param raw: Names as given by the user (already split on commas).
    :returns: The selected types in canonical order, de-duplicated.
    :raises ValueError: If a name is not in :data:`CHART_TYPES`.
    """
    names = [name.strip().lower() for name in raw if name.strip()]
    unknown = [name for name in names if name not in CHART_TYPES]
    if unknown:
        raise ValueError(f"Unknown chart type(s) {', '.join(unknown)}; use any of {', '.join(CHART_TYPES)}")
    return tuple(name for name in CHART_TYPES if name in names) if names else CHART_TYPES


def prepare_plot_dir(directory: str) -> Path:
    """Create *directory* (and parents) up front so a bad path fails before linting.

    :raises OSError: If the directory cannot be created.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    return target


class StatisticsPlotter:
    """Render the chart types of one statistics document to PNG files.

    :param statistics: The mapping returned by :func:`flakeforge.telemetry.compute_statistics`.
    """

    def __init__(self, statistics: Mapping[str, Any]):
        """Bind the statistics document and the chart-type -> drawer table."""
        self._stats = statistics
        self._drawers: dict[str, Callable[[], Figure]] = {
            "rules": self._rules,
            "hit-rate": self._hit_rate,
            "classes": self._classes,
            "outcomes": self._outcomes,
            "models": self._models,
            "top-files": self._top_files,
        }

    def render(self, directory: Path, chart_types: Sequence[str] = CHART_TYPES) -> list[Path]:
        """Write ``<type>.png`` under *directory* for each of *chart_types*.

        :returns: The written paths, in the order of *chart_types*.
        :raises ValueError: If a type is not in :data:`CHART_TYPES`.
        """
        written: list[Path] = []
        for chart_type in chart_types:
            if chart_type not in self._drawers:
                raise ValueError(f"Unknown chart type {chart_type!r}; use any of {', '.join(CHART_TYPES)}")
            path = directory / f"{chart_type}.png"
            self._drawers[chart_type]().savefig(path, dpi=_DPI)
            written.append(path)
        return written

    @staticmethod
    def _bars(
        title: str,
        labels: Sequence[str],
        values: Sequence[float],
        *,
        label: str,
        horizontal: bool = True,
        color: str = _COLOR,
        percent: bool = False,
    ) -> Figure:
        """Build one bar chart, or a ``no data`` placeholder when *labels* is empty."""
        height = max(3.0, 0.32 * len(labels) + 1.4) if horizontal else 4.5
        fig = Figure(figsize=(9, height), layout="constrained")
        ax = fig.subplots()
        ax.set_title(title)
        if not labels:
            ax.text(0.5, 0.5, "no data", ha="center", va="center", transform=ax.transAxes)
            ax.set_axis_off()
        elif horizontal:
            ordered_labels, ordered_values = list(labels)[::-1], list(values)[::-1]
            ax.barh(ordered_labels, ordered_values, color=color)
            ax.set_xlabel(label)
            for y, value in enumerate(ordered_values):
                ax.text(value, y, f" {value:.2%}" if percent else f" {value:g}", va="center", fontsize=8)
        else:
            ax.bar(list(labels), list(values), color=color)
            ax.set_ylabel(label)
        return fig

    def _rules(self) -> Figure:
        """Chart the findings count of every rule that fired."""
        rows = [row for row in self._stats["by_rule"] if row["findings"]]
        return self._bars(
            "Findings by rule", [r["rule"] for r in rows], [r["findings"] for r in rows], label="findings"
        )

    def _hit_rate(self) -> Figure:
        """Chart findings per evaluation for every rule that fired."""
        rows = sorted(
            (row for row in self._stats["by_rule"] if row["hit_rate"]),
            key=lambda r: (-r["hit_rate"], r["rule"]),
        )
        return self._bars(
            "Hit rate by rule (findings / evaluations)",
            [r["rule"] for r in rows],
            [r["hit_rate"] for r in rows],
            label="hit rate",
            color=_ACCENT,
            percent=True,
        )

    def _classes(self) -> Figure:
        """Chart findings per fault class."""
        rows = self._stats["by_class"]
        return self._bars(
            "Findings by fault class",
            [r["fault_class"] for r in rows],
            [r["findings"] for r in rows],
            label="findings",
        )

    def _outcomes(self) -> Figure:
        """Chart event counts per outcome."""
        items = list(self._stats["by_outcome"].items())
        return self._bars(
            "Events by outcome",
            [name for name, _ in items],
            [count for _, count in items],
            label="events",
            horizontal=False,
        )

    def _models(self) -> Figure:
        """Chart findings per generator model."""
        rows = self._stats["by_model"]
        return self._bars(
            "Findings by generator model",
            [str(r["model"] or "unknown") for r in rows],
            [r["findings"] for r in rows],
            label="findings",
            horizontal=False,
        )

    def _top_files(self) -> Figure:
        """Chart the files with the most problems."""
        rows = self._stats["top_files"]
        return self._bars(
            "Files with the most problems",
            [r["path"] for r in rows],
            [r["problem_count"] for r in rows],
            label="problems",
        )


def write_plots(directory: str, chart_types: Sequence[str], statistics: Mapping[str, Any]):
    """Render the selected charts into *directory*.

    Observational like the other telemetry artifacts: a failure is logged at
    warning level and swallowed so it can never change a lint result or exit code.
    """
    try:
        StatisticsPlotter(statistics).render(Path(directory), chart_types)
    except Exception as exc:  # noqa: X002
        _LOGGER.warning("Telemetry plots failed: %s", exc)
