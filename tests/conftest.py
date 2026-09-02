"""Test-wide controls for running Covenant Radar tests without external network access."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Generator
from typing import Any

import pytest

# Set before any test module imports the application, because
# `config.settings` validates the process configuration at import time and a
# developer's `.env` would otherwise decide what the suite runs against — a
# live model provider on one machine and the offline defaults on another.
# A fixture is too late: collection imports test modules first.
os.environ.setdefault("COVENANT_RADAR_DOTENV", "0")


class OutboundNetworkBlocked(RuntimeError):
    """Raised when a test attempts to connect to a non-local network address."""


_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_GETHOSTBYADDR = socket.gethostbyaddr
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})


def _is_local_host(host: object) -> bool:
    """Return whether *host* names an interface on this machine."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if not isinstance(host, str):
        return False

    normalized = host.removesuffix(".").lower().split("%", maxsplit=1)[0]
    if normalized in _LOCAL_HOSTNAMES:
        return True

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    mapped_address = getattr(address, "ipv4_mapped", None)
    if mapped_address is not None:
        return mapped_address.is_loopback or mapped_address.is_unspecified
    return address.is_loopback or address.is_unspecified


def _address_host(address: object) -> object:
    """Extract the host portion of an internet socket address."""
    if isinstance(address, tuple) and address:
        return address[0]
    return address


def _network_error(host: object) -> OutboundNetworkBlocked:
    return OutboundNetworkBlocked(f"Outbound network access is blocked during tests: {host!r}")


class _OfflineSocket(_ORIGINAL_SOCKET):
    """Socket implementation that permits only local connections in tests."""

    def _check_address(self, address: object) -> None:
        if self.family == getattr(socket, "AF_UNIX", None):
            return
        host = _address_host(address)
        if not _is_local_host(host):
            raise _network_error(host)

    def connect(self, address: object) -> None:
        self._check_address(address)
        super().connect(address)

    def connect_ex(self, address: object) -> int:
        self._check_address(address)
        return super().connect_ex(address)

    def sendto(self, data: bytes, address: object) -> int:
        self._check_address(address)
        return super().sendto(data, address)


def _guarded_getaddrinfo(
    host: str | bytes | None,
    port: str | int | None,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[Any, ...]]:
    if not _is_local_host(host):
        raise _network_error(host)
    return _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


def _guarded_gethostbyname(host: str) -> str:
    if not _is_local_host(host):
        raise _network_error(host)
    return _ORIGINAL_GETHOSTBYNAME(host)


def _guarded_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
    if not _is_local_host(host):
        raise _network_error(host)
    return _ORIGINAL_GETHOSTBYNAME_EX(host)


def _guarded_gethostbyaddr(host: str) -> tuple[str, list[str], list[str]]:
    if not _is_local_host(host):
        raise _network_error(host)
    return _ORIGINAL_GETHOSTBYADDR(host)


@pytest.fixture(scope="session", autouse=True)
def block_outbound_network() -> Generator[None]:
    """Install the offline guard for the entire test session."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(socket, "socket", _OfflineSocket)
    patcher.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    patcher.setattr(socket, "gethostbyname", _guarded_gethostbyname)
    patcher.setattr(socket, "gethostbyname_ex", _guarded_gethostbyname_ex)
    patcher.setattr(socket, "gethostbyaddr", _guarded_gethostbyaddr)
    try:
        yield
    finally:
        patcher.undo()
