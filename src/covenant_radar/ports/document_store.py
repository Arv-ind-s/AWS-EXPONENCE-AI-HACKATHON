"""The storage boundary for uploaded documents.

The application persists document metadata separately from the bytes.  This
port deliberately exposes the storage key, rather than a filesystem path, so
the service cannot become coupled to a particular deployment backend.  A
backend must keep ``stream`` incremental: callers use it for downloads and
must not need to materialise a potentially large document in memory.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, Protocol, runtime_checkable


@runtime_checkable
class DocumentStore(Protocol):
    """Content-addressed, encrypted storage for document bytes."""

    def put(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_hash: str | None = None,
    ) -> str:
        """Store content and return its stable content-addressed key."""
        ...

    def get(self, storage_key: str) -> bytes:
        """Return one complete object, or raise ``NotFound``."""
        ...

    def delete(self, storage_key: str) -> None:
        """Delete one object, or raise ``NotFound`` when it is absent."""
        ...

    def stream(self, storage_key: str, *, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        """Yield decrypted plaintext chunks without loading the object whole."""
        ...


__all__ = ["DocumentStore"]
