"""HTMX web presentation layer."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Lazily expose the ASGI factory without creating an import cycle."""
    from covenant_radar.asgi import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
