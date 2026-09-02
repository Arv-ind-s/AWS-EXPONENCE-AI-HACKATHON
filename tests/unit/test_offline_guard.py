"""Tests for the global offline test-session guard."""

from __future__ import annotations

import socket

import pytest

from tests.conftest import OutboundNetworkBlocked


def test_outbound_socket_raises() -> None:
    with pytest.raises(OutboundNetworkBlocked, match="Outbound network access is blocked"):
        socket.create_connection(("example.com", 443), timeout=0.1)


def test_loopback_allowed() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        with socket.create_connection(server.getsockname(), timeout=1) as client:
            connection, _ = server.accept()
            with connection:
                client.sendall(b"offline")
                assert connection.recv(7) == b"offline"
