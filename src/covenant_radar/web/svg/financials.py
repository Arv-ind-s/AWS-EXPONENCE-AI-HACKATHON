"""Accessible inline SVG for a filed financial series.

The renderer is presentation-only, in exactly the sense `svg/trajectory.py`
is: it maps values that are already persisted — statement lines as filed, or
covenant test values as computed and stored by the engine — onto SVG
coordinates. It never interpolates a missing period, extrapolates beyond the
last filed one, or computes a business value of its own.

The difference from `trajectory.py` is what is being plotted. A trajectory is
a *projection*: a dense daily path into the future, ruled at the horizons the
forecast was made for. A series here is a *record*: one point per filed
financial period, evenly spaced regardless of the calendar gap between them,
because a reader comparing eight quarters is comparing the quarters, not the
days between them. Sharing one renderer between the two would mean one of
them lying about its own x-axis, so they stay separate.

A threshold is optional and has no default: a covenanted ratio has one and a
statement line does not, and a renderer that invented a threshold line for
revenue would be drawing a limit that no agreement contains.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Final

from markupsafe import Markup

_ZERO: Final[Decimal] = Decimal("0")
_VIEWBOX_WIDTH: Final[Decimal] = Decimal("100")
_VIEWBOX_HEIGHT: Final[Decimal] = Decimal("32")
_PLOT_X: Final[Decimal] = Decimal("3")
_PLOT_TOP: Final[Decimal] = Decimal("3")
_PLOT_BOTTOM: Final[Decimal] = Decimal("29")
_PLOT_WIDTH: Final[Decimal] = _VIEWBOX_WIDTH - (_PLOT_X * Decimal("2"))
_PLOT_HEIGHT: Final[Decimal] = _PLOT_BOTTOM - _PLOT_TOP
_PLOT_PADDING_RATIO: Final[Decimal] = Decimal("0.08")
_MIN_VALUE_PADDING: Final[Decimal] = Decimal("0.5")
_COORDINATE_QUANTUM: Final[Decimal] = Decimal("0.01")
_LATEST_MARKER_RADIUS: Final[str] = "1.6"
_BREACH_MARKER_RADIUS: Final[str] = "2"

_REFUSAL_POINTS = "Trend unavailable: at least two filed periods are required to draw a series."


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One filed period's value, with the period label its marker announces.

    `is_breach` is supplied by the caller from the stored covenant verdict —
    never inferred here by comparing `value` against `threshold`. Which side
    of a threshold counts as a breach, whether a cure window is open, and
    whether the period was even testable are all facts the engine already
    settled and persisted; re-deciding them at the rendering boundary is how
    a chart comes to disagree with the ledger beside it.
    """

    label: str
    value: Decimal
    is_breach: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("A series point requires a non-empty period label.")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("A series point value must be a finite Decimal.")


def render_series_svg(
    series_id: str,
    points: Sequence[SeriesPoint],
    *,
    label: str,
    value_labels: Sequence[str] = (),
    threshold: Decimal | None = None,
    threshold_label: str = "",
    breach_above: bool | None = None,
) -> Markup:
    """Return a safe SVG for one filed series, or a refusal state.

    ``value_labels`` are the already-formatted display strings for each
    point, in the same order. They are read into ``<desc>`` so a screen
    reader receives the figures themselves rather than "a line chart" — the
    same text-equivalent rule `trajectory.py` enforces through its ledger.
    Formatting stays with the caller, which knows whether a series is rupees,
    a multiple or a percentage; this module never formats a number it is
    asked to plot.

    ``breach_above`` shades the side of ``threshold`` on which the covenant
    is in breach. Like `trajectory.py`'s, it has no default: which way a
    covenant breaches is a term of the covenant, not something to guess.
    """

    safe_id = _required_text(series_id, "series_id")
    safe_label = _required_text(label, "label")
    normalized = _normalize_points(points)
    if len(normalized) < 2:
        return _refusal(_REFUSAL_POINTS)

    normalized_threshold = _optional_decimal(threshold)
    plot = _plot_coordinates(normalized, normalized_threshold)
    escaped_id = escape(safe_id, quote=True)

    described = ", ".join(
        f"{point.label} {display}" for point, display in zip(normalized, value_labels, strict=False)
    ) or ", ".join(f"{point.label} {_plain(point.value)}" for point in normalized)
    description = f"{safe_label}. Filed periods, earliest first: {described}."
    if normalized_threshold is not None and threshold_label.strip():
        description += f" Threshold: {threshold_label.strip()}."
    breached = tuple(point.label for point in normalized if point.is_breach)
    if breached:
        description += " In breach at: " + ", ".join(breached) + "."

    points_text = " ".join(f"{_coordinate_text(x)},{_coordinate_text(y)}" for x, y in plot.points)
    threshold_markup = ""
    if plot.threshold_y is not None:
        threshold_y = _coordinate_text(plot.threshold_y)
        threshold_markup = (
            f"{_breach_zone(plot.threshold_y, breach_above)}"
            f'<line class="series__threshold" x1="{_coordinate_text(_PLOT_X)}" '
            f'y1="{threshold_y}" x2="{_coordinate_text(_VIEWBOX_WIDTH - _PLOT_X)}" '
            f'y2="{threshold_y}"></line>'
        )

    view_box = f"0 0 {_coordinate_text(_VIEWBOX_WIDTH)} {_coordinate_text(_VIEWBOX_HEIGHT)}"
    return Markup(
        f'<svg class="series__plot" viewBox="{view_box}" role="img" '
        f'aria-labelledby="{escaped_id}-title" '
        f'aria-describedby="{escaped_id}-description" focusable="false" '
        f'preserveAspectRatio="none" data-series="filed">'
        f'<title id="{escaped_id}-title">{escape(safe_label)}</title>'
        f'<desc id="{escaped_id}-description">{escape(description)}</desc>'
        f"{threshold_markup}"
        f'<path class="series__area" d="{_area_path(plot.points)}"></path>'
        f'<polyline class="series__line" points="{points_text}"></polyline>'
        f"{_breach_markers(normalized, plot)}"
        f'<circle class="series__latest" cx="{_coordinate_text(plot.points[-1][0])}" '
        f'cy="{_coordinate_text(plot.points[-1][1])}" r="{_LATEST_MARKER_RADIUS}"></circle>'
        "</svg>"
    )


def _normalize_points(points: Sequence[SeriesPoint]) -> tuple[SeriesPoint, ...]:
    if not isinstance(points, Sequence) or isinstance(points, str | bytes):
        return ()
    for point in points:
        if not isinstance(point, SeriesPoint):
            return ()
    return tuple(points)


@dataclass(frozen=True, slots=True)
class _Plot:
    points: tuple[tuple[Decimal, Decimal], ...]
    threshold_y: Decimal | None


def _plot_coordinates(points: tuple[SeriesPoint, ...], threshold: Decimal | None) -> _Plot:
    """Scale the series to the plot box, including the threshold in the extent.

    A threshold outside the series' own range still has to be visible — a
    covenant tested at 3.00x against a ratio that never left 1.2-1.4x is a
    reader's single most useful piece of context, and clipping it off the top
    of the plot would show a comfortable-looking flat line with no limit in
    sight.
    """

    values = [point.value for point in points]
    if threshold is not None:
        values.append(threshold)
    minimum = min(values)
    maximum = max(values)
    span = maximum - minimum
    if span == _ZERO:
        padding = max(abs(maximum) * _PLOT_PADDING_RATIO, _MIN_VALUE_PADDING)
        minimum -= padding
        maximum += padding
        span = maximum - minimum
    else:
        padding = span * _PLOT_PADDING_RATIO
        minimum -= padding
        maximum += padding
        span = maximum - minimum

    def y_coordinate(value: Decimal) -> Decimal:
        return _PLOT_BOTTOM - (((value - minimum) / span) * _PLOT_HEIGHT)

    # Filed periods are evenly spaced: the reader is comparing quarters, not
    # the number of days between the dates they closed on.
    last_index = Decimal(len(points) - 1)
    coordinates = tuple(
        (_PLOT_X + (Decimal(index) / last_index * _PLOT_WIDTH), y_coordinate(point.value))
        for index, point in enumerate(points)
    )
    return _Plot(
        points=coordinates,
        threshold_y=y_coordinate(threshold) if threshold is not None else None,
    )


def _breach_markers(points: tuple[SeriesPoint, ...], plot: _Plot) -> str:
    return "".join(
        f'<circle class="series__breach" cx="{_coordinate_text(plot.points[index][0])}" '
        f'cy="{_coordinate_text(plot.points[index][1])}" '
        f'r="{_BREACH_MARKER_RADIUS}"><title>{escape(point.label)}: in breach</title></circle>'
        for index, point in enumerate(points)
        if point.is_breach
    )


def _breach_zone(threshold_y: Decimal, breach_above: bool | None) -> str:
    """Shade the breaching side of the threshold; draw nothing without a side."""

    if breach_above is None:
        return ""
    top = _PLOT_TOP if breach_above else threshold_y
    bottom = threshold_y if breach_above else _PLOT_BOTTOM
    height = bottom - top
    if height <= _ZERO:
        return ""
    return (
        f'<rect class="series__breach-zone" x="{_coordinate_text(_PLOT_X)}" '
        f'y="{_coordinate_text(top)}" width="{_coordinate_text(_PLOT_WIDTH)}" '
        f'height="{_coordinate_text(height)}"></rect>'
    )


def _area_path(points: tuple[tuple[Decimal, Decimal], ...]) -> str:
    if not points:
        return ""
    floor = _coordinate_text(_PLOT_BOTTOM)
    segments = [f"M {_coordinate_text(points[0][0])} {_coordinate_text(points[0][1])}"]
    segments.extend(f"L {_coordinate_text(x)} {_coordinate_text(y)}" for x, y in points[1:])
    segments.append(f"L {_coordinate_text(points[-1][0])} {floor}")
    segments.append(f"L {_coordinate_text(points[0][0])} {floor}")
    segments.append("Z")
    return " ".join(segments)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text.")
    return value.strip()


def _coordinate_text(value: Decimal) -> str:
    text = format(value.quantize(_COORDINATE_QUANTUM), "f").rstrip("0").rstrip(".")
    return text or "0"


def _plain(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
    return text or "0"


def _refusal(message: str) -> Markup:
    return Markup(f'<p class="series-state series-state--refused">{escape(message)}</p>')


__all__ = ["SeriesPoint", "render_series_svg"]
