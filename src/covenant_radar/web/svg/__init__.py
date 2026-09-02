"""Safe, record-backed SVG renderers for browser surfaces."""

from covenant_radar.web.svg.trajectory import (
    TrajectoryCrossing,
    TrajectoryLedgerFigure,
    TrajectoryPoint,
    build_trajectory_sparkline_svg,
    build_trajectory_svg,
    render_trajectory_sparkline_svg,
    render_trajectory_svg,
    trajectory_sparkline_svg,
    trajectory_svg,
)

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
