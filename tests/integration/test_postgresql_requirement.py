"""Integration checks that require the PostgreSQL service supplied by CI."""

from __future__ import annotations

import os

import psycopg
import pytest


@pytest.mark.integration
def test_postgresql_service_is_required() -> None:
    database_url = os.environ.get("COVENANT_RADAR_DATABASE_URL")
    if database_url is None:
        pytest.fail(
            "PostgreSQL is required for integration tests; COVENANT_RADAR_DATABASE_URL is unset."
        )

    try:
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
    except psycopg.OperationalError:
        pytest.fail("PostgreSQL is required for integration tests but is unavailable.")
