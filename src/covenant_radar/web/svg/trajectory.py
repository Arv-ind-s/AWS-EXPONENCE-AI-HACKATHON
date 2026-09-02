"""Accessible inline SVG for a persisted covenant trajectory.

The renderer is deliberately presentation-only.  It accepts the daily path
already persisted by the forecast run and maps those values to SVG
coordinates; it never projects, interpolates a missing business value, or
calls a provider.  A chart is refused unless its ledger figures are present,
which keeps the ``spec §15.2`` no-chart-without-ledger rule enforceable at the
lowest rendering boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Final

from markupsafe import Markup

_ZERO: Final[Decimal] = Decimal("0")
_VIEWBOX_WIDTH: Final[Decimal] = Decimal("100")
_VIEWBOX_HEIGHT: Final[Decimal] = Decimal("40")
_PLOT_X: Final[Decimal] = Decimal("2")
_PLOT_TOP: Final[Decimal] = Decimal("2")
_PLOT_BOTTOM: Final[Decimal] = Decimal("38")
_PLOT_WIDTH: Final[Decimal] = _VIEWBOX_WIDTH - (_PLOT_X * Decimal("2"))
_PLOT_HEIGHT: Final[Decimal] = _PLOT_BOTTOM - _PLOT_TOP
_PLOT_PADDING_RATIO: Final[Decimal] = Decimal("0.05")
_MIN_VALUE_PADDING: Final[Decimal] = Decimal("1")
_COORDINATE_QUANTUM: Final[Decimal] = Decimal("0.01")
# The crossing label is drawn in the plot's own user space, so its size and its
# length are both bounded in viewBox units.  Keep the font size in step with
# ``.trajectory__crossing-annotation`` in ``forecast.css``.
_ANNOTATION_FONT_SIZE: Final[Decimal] = Decimal("2.6")
_ANNOTATION_CHAR_WIDTH: Final[Decimal] = _ANNOTATION_FONT_SIZE * Decimal("0.55")
_ANNOTATION_OFFSET: Final[Decimal] = Decimal("2")
_ANNOTATION_MAX_CHARS: Final[int] = 34
# The horizons the case file forecasts on, ruled inside the plot so the path
# has a day scale.  A stop is drawn only when the stored path actually spans
# it, so a thirty-day path is not labelled with days it does not contain.
_GRID_DAYS: Final[tuple[int, ...]] = (30, 60, 90)
_GRID_LABEL_INSET: Final[Decimal] = Decimal("1")
_REFUSAL_LEDGER = "Trajectory unavailable: ledger figures are required before a chart can be shown."
_REFUSAL_PATH = "Trajectory unavailable: the stored daily path is missing or incomplete."


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    """One persisted daily path point used for plot coordinates."""

    day: int
    value: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.day, bool) or not isinstance(self.day, int) or self.day < 0:
            raise ValueError("Trajectory point day must be a non-negative integer.")
        if not isinstance(self.value, Decimal) or not self.value.is_finite():
            raise ValueError("Trajectory point value must be a finite Decimal.")


@dataclass(frozen=True, slots=True)
class TrajectoryCrossing:
    """A crossing day and its already-formatted persisted date label."""

    day: int
    date_label: str
    label: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.day, bool) or not isinstance(self.day, int) or self.day < 0:
            raise ValueError("Trajectory crossing day must be a non-negative integer.")
        if not isinstance(self.date_label, str) or not self.date_label.strip():
            raise ValueError("Trajectory crossing date label must be non-empty text.")
        if not isinstance(self.label, str):
            raise TypeError("Trajectory crossing label must be text.")


@dataclass(frozen=True, slots=True)
class TrajectoryLedgerFigure:
    """One text figure that sits beside and describes the trajectory."""

    label: str
    value: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Trajectory ledger figure label must be non-empty text.")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Trajectory ledger figure value must be non-empty text.")
        if not isinstance(self.detail, str):
            raise TypeError("Trajectory ledger figure detail must be text.")


def render_trajectory_svg(
    trajectory_id: str,
    points: Sequence[TrajectoryPoint | Mapping[str, object] | Sequence[object] | object],
    threshold: Decimal,
    ledger_figures: Sequence[TrajectoryLedgerFigure | Mapping[str, object] | Sequence[object]],
    *,
    crossing: TrajectoryCrossing | None = None,
    label: str = "Covenant trajectory",
    breach_above: bool | None = None,
) -> Markup:
    """Return a safe SVG, or a refusal state when the data is not drawable.

    ``ledger_figures`` is required even though the table is rendered by the
    caller.  The renderer uses it to enforce the pairing rule and repeats its
    text in ``<desc>`` so screen-reader users receive the same text equivalent
    as the visible ledger.

    ``breach_above`` states which side of the threshold a breach lives on so
    that side can be shaded.  It is optional and has no default side: which
    way a covenant breaches is a fact about the covenant, and this renderer
    does not infer business values it was not given.
    """

    safe_id = _required_text(trajectory_id, "trajectory_id")
    safe_label = _required_text(label, "label")
    normalized_figures = _normalize_ledger_figures(ledger_figures)
    if not normalized_figures:
        return _refusal(_REFUSAL_LEDGER)

    normalized_points = _normalize_points(points)
    if len(normalized_points) < 2:
        return _refusal(_REFUSAL_PATH)

    normalized_threshold = _decimal(threshold, "threshold")
    plot = _plot_coordinates(normalized_points, normalized_threshold)
    crossing_coordinate = _crossing_coordinate(
        normalized_points,
        plot,
        crossing,
    )
    description = "; ".join(
        f"{figure.label}: {figure.value}" + (f" ({figure.detail})" if figure.detail.strip() else "")
        for figure in normalized_figures
    )
    if crossing_coordinate is not None and crossing is not None:
        description += f"; projected crossing on day {crossing.day}, {crossing.date_label}" + (
            f"; {crossing.label}" if crossing.label.strip() else ""
        )
    escaped_id = escape(safe_id, quote=True)
    escaped_label = escape(safe_label)
    escaped_description = escape(f"{safe_label}. {description}")
    threshold_y = _coordinate_text(plot.threshold_y)
    points_text = " ".join(f"{_coordinate_text(x)},{_coordinate_text(y)}" for x, y in plot.points)

    crossing_markup = ""
    if crossing_coordinate is not None and crossing is not None:
        crossing_x, crossing_y = crossing_coordinate
        crossing_markup = (
            f'<line class="trajectory__crossing-tick" '
            f'x1="{_coordinate_text(crossing_x)}" y1="{_coordinate_text(_PLOT_TOP)}" '
            f'x2="{_coordinate_text(crossing_x)}" y2="{_coordinate_text(_PLOT_BOTTOM)}" '
            f'data-crossing-day="{crossing.day}" '
            f'data-crossing-date="{escape(crossing.date_label, quote=True)}" '
            f'data-crossing-label="{escape(crossing.label, quote=True)}"></line>'
            f'<circle class="trajectory__crossing" '
            f'cx="{_coordinate_text(crossing_x)}" cy="{_coordinate_text(crossing_y)}" '
            f'r="1.5"></circle>'
        )
        crossing_markup += _crossing_annotation(crossing, crossing_x)

    view_box = "0 0 " + _coordinate_text(_VIEWBOX_WIDTH) + " " + _coordinate_text(_VIEWBOX_HEIGHT)
    return Markup(
        f'<svg class="trajectory__plot" viewBox="{view_box}" role="img" '
        f'aria-labelledby="{escaped_id}-title" '
        f'aria-describedby="{escaped_id}-ledger {escaped_id}-description" focusable="false" '
        f'data-trajectory="stored" '
        # The plot's own vertical extent, stated once on the root so script
        # never has to infer it.  `horizon.js` used to take it from the
        # threshold line, which is horizontal, so every marker it drew came
        # out zero-height and therefore invisible.
        f'data-plot-top="{_coordinate_text(_PLOT_TOP)}" '
        f'data-plot-bottom="{_coordinate_text(_PLOT_BOTTOM)}">'
        f'<title id="{escaped_id}-title">{escaped_label}</title>'
        f'<desc id="{escaped_id}-description">{escaped_description}</desc>'
        f"{_breach_zone(plot.threshold_y, breach_above)}"
        f"{_gridlines(plot)}"
        f'<path class="trajectory__area" d="{_area_path(plot.points)}"></path>'
        f'<line class="trajectory__threshold" x1="{_coordinate_text(_PLOT_X)}" '
        f'y1="{threshold_y}" x2="{_coordinate_text(_VIEWBOX_WIDTH - _PLOT_X)}" '
        f'y2="{threshold_y}"></line>'
        f'<polyline class="trajectory__line" points="{points_text}"></polyline>'
        f"{crossing_markup}"
        "</svg>"
    )


def render_trajectory_sparkline_svg(
    trajectory_id: str,
    points: Sequence[TrajectoryPoint | Mapping[str, object] | Sequence[object] | object],
    threshold: Decimal,
    *,
    label: str = "Stored covenant trajectory",
) -> Markup:
    """Return a compact, accessible rendering of a persisted daily path.

    Sparklines intentionally omit the ledger pairing used by the full case
    file. Their text equivalent names the stored path and threshold, while
    the queue row supplies the surrounding borrower context.
    """

    safe_id = _required_text(trajectory_id, "trajectory_id")
    safe_label = _required_text(label, "label")
    normalized_points = _normalize_points(points)
    if len(normalized_points) < 2:
        return _refusal(_REFUSAL_PATH)
    normalized_threshold = _decimal(threshold, "threshold")
    plot = _plot_coordinates(normalized_points, normalized_threshold)
    escaped_id = escape(safe_id, quote=True)
    escaped_label = escape(safe_label)
    description = escape(
        f"{safe_label}. Stored daily path from day {plot.minimum_day} to "
        f"day {plot.maximum_day}; threshold: {normalized_threshold}."
    )
    points_text = " ".join(f"{_coordinate_text(x)},{_coordinate_text(y)}" for x, y in plot.points)
    return Markup(
        f'<svg class="trajectory__plot trajectory__plot--mini" '
        f'viewBox="0 0 100 40" role="img" '
        f'aria-labelledby="{escaped_id}-title" '
        f'aria-describedby="{escaped_id}-description" focusable="false" '
        f'data-trajectory="stored" data-trajectory-size="mini">'
        f'<title id="{escaped_id}-title">{escaped_label}</title>'
        f'<desc id="{escaped_id}-description">{description}</desc>'
        f'<line class="trajectory__threshold" x1="{_coordinate_text(_PLOT_X)}" '
        f'y1="{_coordinate_text(plot.threshold_y)}" '
        f'x2="{_coordinate_text(_VIEWBOX_WIDTH - _PLOT_X)}" '
        f'y2="{_coordinate_text(plot.threshold_y)}"></line>'
        # The area fill alone: a sparkline is a shape, not a chart, so it
        # gets no gridlines, no day labels and no breach shading.
        f'<path class="trajectory__area" d="{_area_path(plot.points)}"></path>'
        f'<polyline class="trajectory__line" points="{points_text}"></polyline>'
        "</svg>"
    )


def _normalize_points(
    points: Sequence[TrajectoryPoint | Mapping[str, object] | Sequence[object] | object],
) -> tuple[TrajectoryPoint, ...]:
    if not isinstance(points, Sequence) or isinstance(points, str | bytes):
        return ()
    normalized: list[TrajectoryPoint] = []
    for point in points:
        try:
            normalized.append(_point(point))
        except (TypeError, ValueError, InvalidOperation):
            return ()
    if not normalized or normalized[0].day != 0:
        return ()
    if any(
        current.day <= previous.day
        for previous, current in zip(normalized, normalized[1:], strict=False)
    ):
        return ()
    return tuple(normalized)


def _point(
    value: TrajectoryPoint | Mapping[str, object] | Sequence[object] | object,
) -> TrajectoryPoint:
    if isinstance(value, TrajectoryPoint):
        return value
    if isinstance(value, Mapping):
        day = value.get("day", value.get("day_offset"))
        point_value = value.get("value", value.get("projected_value"))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        if len(value) < 2:
            raise ValueError("Trajectory point sequences require day and value.")
        day, point_value = value[0], value[1]
    else:
        day = getattr(value, "day", getattr(value, "day_offset", None))
        point_value = getattr(value, "value", getattr(value, "projected_value", None))
    if isinstance(day, bool) or not isinstance(day, int):
        raise TypeError("Trajectory point day must be an integer.")
    return TrajectoryPoint(day=day, value=_decimal(point_value, "trajectory point value"))


def _normalize_ledger_figures(
    figures: Sequence[TrajectoryLedgerFigure | Mapping[str, object] | Sequence[object]],
) -> tuple[TrajectoryLedgerFigure, ...]:
    if not isinstance(figures, Sequence) or isinstance(figures, str | bytes):
        return ()
    normalized: list[TrajectoryLedgerFigure] = []
    for figure in figures:
        try:
            normalized.append(_ledger_figure(figure))
        except (TypeError, ValueError):
            return ()
    return tuple(normalized)


def _ledger_figure(
    value: TrajectoryLedgerFigure | Mapping[str, object] | Sequence[object],
) -> TrajectoryLedgerFigure:
    if isinstance(value, TrajectoryLedgerFigure):
        return value
    if isinstance(value, Mapping):
        return TrajectoryLedgerFigure(
            label=_required_text(value.get("label"), "ledger figure label"),
            value=_required_text(value.get("value"), "ledger figure value"),
            detail=str(value.get("detail", "")),
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes) and len(value) >= 2:
        return TrajectoryLedgerFigure(
            label=_required_text(value[0], "ledger figure label"),
            value=_required_text(value[1], "ledger figure value"),
            detail=str(value[2]) if len(value) > 2 else "",
        )
    raise TypeError("Trajectory ledger figures require label and value.")


@dataclass(frozen=True, slots=True)
class _Plot:
    points: tuple[tuple[Decimal, Decimal], ...]
    threshold_y: Decimal
    minimum_day: int
    maximum_day: int


def _plot_coordinates(points: tuple[TrajectoryPoint, ...], threshold: Decimal) -> _Plot:
    values = [point.value for point in points]
    minimum = min(*values, threshold)
    maximum = max(*values, threshold)
    span = maximum - minimum
    if span == _ZERO:
        padding = max(abs(maximum) * _PLOT_PADDING_RATIO, _MIN_VALUE_PADDING)
        minimum -= padding
        maximum += padding
        span = maximum - minimum

    def y_coordinate(value: Decimal) -> Decimal:
        fraction = (value - minimum) / span
        return _PLOT_BOTTOM - (fraction * _PLOT_HEIGHT)

    minimum_day = points[0].day
    maximum_day = points[-1].day
    day_span = Decimal(maximum_day - minimum_day)
    coordinates = tuple(
        (
            _PLOT_X + (Decimal(point.day - minimum_day) / day_span * _PLOT_WIDTH),
            y_coordinate(point.value),
        )
        for point in points
    )
    return _Plot(
        points=coordinates,
        threshold_y=y_coordinate(threshold),
        minimum_day=minimum_day,
        maximum_day=maximum_day,
    )


def _crossing_coordinate(
    points: tuple[TrajectoryPoint, ...],
    plot: _Plot,
    crossing: TrajectoryCrossing | None,
) -> tuple[Decimal, Decimal] | None:
    if crossing is None or not plot.minimum_day <= crossing.day <= plot.maximum_day:
        return None
    if crossing.day == plot.minimum_day:
        return plot.points[0]
    if crossing.day == plot.maximum_day:
        return plot.points[-1]
    for index, point in enumerate(points[:-1]):
        next_point = points[index + 1]
        if point.day <= crossing.day <= next_point.day:
            first = plot.points[index]
            second = plot.points[index + 1]
            day_span = Decimal(next_point.day - point.day)
            ratio = Decimal(crossing.day - point.day) / day_span
            return first[0] + ((second[0] - first[0]) * ratio), first[1] + (
                (second[1] - first[1]) * ratio
            )
    return None


def _decimal(value: object, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise TypeError(f"{field} must be a finite Decimal.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a finite Decimal.") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite Decimal.")
    return result


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text.")
    return value.strip()


def _coordinate_text(value: Decimal) -> str:
    rendered = value.quantize(_COORDINATE_QUANTUM)
    text = format(rendered, "f").rstrip("0").rstrip(".")
    return text or "0"


def _refusal(message: str) -> Markup:
    return Markup(
        f'<p class="trajectory-state trajectory-state--refused" role="alert">{escape(message)}</p>'
    )


def _crossing_annotation(crossing: TrajectoryCrossing, x: Decimal) -> str:
    """Build a bounded visible label while retaining full data attributes.

    The label is day and date only.  The dominant-driver clause stays in
    ``<desc>`` and in the attribution list beside the chart: repeated here it
    made a string wider than the whole plot, which the annotation cannot be
    because it is measured and placed in the same user space as the path.
    """

    text = _short_svg_text(
        f"Crossing day {crossing.day} · {crossing.date_label}",
        maximum=_ANNOTATION_MAX_CHARS,
    )
    width = Decimal(len(text)) * _ANNOTATION_CHAR_WIDTH
    right_edge = _VIEWBOX_WIDTH - _PLOT_X
    if x + _ANNOTATION_OFFSET + width <= right_edge:
        anchor = "start"
        position = x + _ANNOTATION_OFFSET
    elif x - _ANNOTATION_OFFSET - width >= _PLOT_X:
        anchor = "end"
        position = x - _ANNOTATION_OFFSET
    else:
        anchor = "start"
        position = _PLOT_X
    return (
        '<text class="trajectory__crossing-annotation" '
        f'x="{_coordinate_text(position)}" y="6" text-anchor="{anchor}" '
        'data-crossing-annotation="true">'
        f"{escape(text, quote=False)}</text>"
    )


def _breach_zone(threshold_y: Decimal, breach_above: bool | None) -> str:
    """Shade the side of the threshold on which the covenant is in breach.

    Higher values sit at smaller ``y``, so a covenant that breaches above its
    threshold shades from the top of the plot down to the threshold line.
    Nothing is drawn when the caller did not state a direction.
    """

    if breach_above is None:
        return ""
    top = _PLOT_TOP if breach_above else threshold_y
    bottom = threshold_y if breach_above else _PLOT_BOTTOM
    height = bottom - top
    if height <= _ZERO:
        return ""
    return (
        '<rect class="trajectory__breach-zone" '
        f'x="{_coordinate_text(_PLOT_X)}" y="{_coordinate_text(top)}" '
        f'width="{_coordinate_text(_PLOT_WIDTH)}" '
        f'height="{_coordinate_text(height)}"></rect>'
    )


def _gridlines(plot: _Plot) -> str:
    """Rule and label the named horizons the case file actually forecasts on.

    Without them the path has no day scale at all, so a reader cannot say
    where in the ninety days a crossing falls.  The labels are sized in the
    plot's own user space for the reason ``_crossing_annotation`` explains.
    """

    span = Decimal(plot.maximum_day - plot.minimum_day)
    if span <= _ZERO:
        return ""
    parts: list[str] = []
    for day in _GRID_DAYS:
        if not plot.minimum_day < day < plot.maximum_day:
            continue
        x = _coordinate_text(_PLOT_X + (Decimal(day - plot.minimum_day) / span * _PLOT_WIDTH))
        parts.append(
            '<line class="trajectory__gridline" '
            f'x1="{x}" y1="{_coordinate_text(_PLOT_TOP)}" '
            f'x2="{x}" y2="{_coordinate_text(_PLOT_BOTTOM)}" '
            f'data-grid-day="{day}"></line>'
            '<text class="trajectory__gridline-label" '
            f'x="{x}" y="{_coordinate_text(_PLOT_BOTTOM - _GRID_LABEL_INSET)}" '
            f'text-anchor="middle">{day}d</text>'
        )
    return "".join(parts)


def _area_path(points: tuple[tuple[Decimal, Decimal], ...]) -> str:
    """Close the stored path down to the plot floor so the series has body.

    The fill is decoration over the same coordinates the polyline already
    uses; it adds no point the path did not contain.
    """

    if not points:
        return ""
    floor = _coordinate_text(_PLOT_BOTTOM)
    segments = [f"M {_coordinate_text(points[0][0])} {_coordinate_text(points[0][1])}"]
    segments.extend(f"L {_coordinate_text(x)} {_coordinate_text(y)}" for x, y in points[1:])
    segments.append(f"L {_coordinate_text(points[-1][0])} {floor}")
    segments.append(f"L {_coordinate_text(points[0][0])} {floor}")
    segments.append("Z")
    return " ".join(segments)


def _short_svg_text(value: str, maximum: int = 72) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1].rstrip() + "…"


# Noun-first aliases keep the small renderer discoverable without introducing
# separate implementations for callers using different project terminology.
trajectory_svg = render_trajectory_svg
build_trajectory_svg = render_trajectory_svg
trajectory_sparkline_svg = render_trajectory_sparkline_svg
build_trajectory_sparkline_svg = render_trajectory_sparkline_svg


__all__ = [
    "TrajectoryCrossing",
    "TrajectoryLedgerFigure",
    "TrajectoryPoint",
    "build_trajectory_sparkline_svg",
    "build_trajectory_svg",
    "render_trajectory_svg",
    "render_trajectory_sparkline_svg",
    "trajectory_svg",
    "trajectory_sparkline_svg",
]
