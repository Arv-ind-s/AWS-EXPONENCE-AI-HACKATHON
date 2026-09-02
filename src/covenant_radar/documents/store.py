"""Encrypted content-addressed document storage.

The local backend stores a small binary envelope followed by independently
authenticated encrypted frames.  Encrypting frames separately lets a reader
stream a document without loading the complete plaintext or ciphertext into
memory.  The content hash is stored in the envelope and is checked again when
the stream reaches EOF, so truncation, substitution, and frame corruption are
reported as storage failures rather than returning a partial document as if it
were complete.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import secrets
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Final

from covenant_radar.core.errors import ExternalServiceError, NotFound
from covenant_radar.ports.document_store import DocumentStore
from covenant_radar.security.crypto import FieldEncryptor

_MAGIC: Final[bytes] = b"CRDOC001"
_HEADER = struct.Struct(">8s32sQ")
_FRAME_LENGTH = struct.Struct(">I")
_DEFAULT_CHUNK_SIZE: Final[int] = 256 * 1024
_MAX_FRAME_BYTES: Final[int] = 8 * 1024 * 1024
_HASH_HEX_LENGTH: Final[int] = hashlib.sha256().digest_size * 2
_STORAGE_PREFIX: Final[str] = "sha256"
_LOGGER = logging.getLogger(__name__)


class StorageUnavailable(ExternalServiceError):
    """The configured document backend could not safely complete an operation."""

    def __init__(self, message: str, *, path: Path | None = None) -> None:
        super().__init__(message, field="documents.storage")
        self.path = path


class FileSystemDocumentStore(DocumentStore):
    """A local, atomically-written, encrypted document store.

    ``encryptor`` must be configured explicitly.  There is no development key
    or plaintext fallback: constructing the adapter without authenticated
    encryption is a configuration error.  Objects are sharded by the first
    four hash characters to avoid a single directory becoming a filesystem
    bottleneck.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        encryptor: FieldEncryptor,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if not isinstance(encryptor, FieldEncryptor):
            raise TypeError("FileSystemDocumentStore requires a FieldEncryptor.")
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
            raise TypeError("Document store chunk_size must be an integer.")
        if not 1 <= chunk_size <= _MAX_FRAME_BYTES:
            raise ValueError(
                f"Document store chunk_size must be between 1 and {_MAX_FRAME_BYTES} bytes."
            )
        if not isinstance(root, str | Path):
            raise TypeError("Document store root must be a path.")
        self.root = Path(root).expanduser().resolve()
        self.encryptor = encryptor
        self.chunk_size = chunk_size

    @staticmethod
    def content_addressed_key(content_hash: str) -> str:
        """Return the canonical storage key for a SHA-256 content hash."""
        digest = _validate_hash(content_hash)
        return f"{_STORAGE_PREFIX}/{digest}"

    def put(
        self,
        content: bytes | bytearray | memoryview | BinaryIO,
        *,
        content_hash: str | None = None,
    ) -> str:
        """Encrypt and atomically store content, returning a stable key.

        The source is consumed in bounded chunks.  The temporary file is
        created below the configured store root and is removed on every
        failure, so a failed write cannot leave plaintext or a half-object in
        the content-addressed namespace.
        """
        source = _binary_source(content)
        self._ensure_directory(self.root)
        staging = self.root / ".staging"
        self._ensure_directory(staging)

        temporary_path: Path | None = None
        descriptor: int | None = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=staging,
                prefix=f".{secrets.token_hex(8)}-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w+b") as target:
                descriptor = None
                target.write(b"\x00" * _HEADER.size)
                while True:
                    block = source.read(self.chunk_size)
                    if block == b"":
                        break
                    if not isinstance(block, bytes):
                        raise TypeError("Document content must be binary.")
                    digest.update(block)
                    byte_count += len(block)
                    # FieldEncryptor's public decrypt contract returns UTF-8
                    # text.  Encode arbitrary document bytes as base64 inside
                    # each authenticated frame so binary PDFs/XLSX files are
                    # supported without reaching into its private key state.
                    encoded_block = base64.b64encode(block).decode("ascii")
                    encrypted = self.encryptor.encrypt(encoded_block)
                    if encrypted is None:
                        raise StorageUnavailable(
                            "Document encryption returned no ciphertext.", path=temporary_path
                        )
                    encoded = encrypted.encode("ascii")
                    if len(encoded) > _MAX_FRAME_BYTES:
                        raise StorageUnavailable(
                            "Document encryption produced an oversized frame.",
                            path=temporary_path,
                        )
                    target.write(_FRAME_LENGTH.pack(len(encoded)))
                    target.write(encoded)

                actual_hash = digest.hexdigest()
                if content_hash is not None and _validate_hash(content_hash) != actual_hash:
                    raise ValueError(
                        "The supplied document content_hash does not match the content bytes."
                    )
                target.seek(0)
                target.write(_HEADER.pack(_MAGIC, digest.digest(), byte_count))
                target.flush()
                os.fsync(target.fileno())

            storage_key = self.content_addressed_key(actual_hash)
            destination = self._path_for_key(storage_key)
            self._ensure_directory(destination.parent)
            if destination.exists():
                if not destination.is_file() or destination.is_symlink():
                    raise StorageUnavailable(
                        "Document storage key resolves to a non-regular file.", path=destination
                    )
                if self._is_valid_existing(destination, storage_key):
                    temporary_path.unlink(missing_ok=True)
                    temporary_path = None
                    return storage_key
            os.replace(temporary_path, destination)
            temporary_path = None
            _fsync_directory(destination.parent)
            return storage_key
        except (StorageUnavailable, TypeError, ValueError):
            raise
        except OSError as error:
            raise StorageUnavailable(
                f"Document storage is unavailable at {self.root}.", path=self.root
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("Unable to remove temporary document file %s", temporary_path)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    _LOGGER.warning("Unable to close temporary document descriptor.")

    def get(self, storage_key: str) -> bytes:
        """Read one complete object through the same verified stream path."""
        return b"".join(self.stream(storage_key))

    def delete(self, storage_key: str) -> None:
        """Remove one stored object after validating its canonical key."""
        path = self._path_for_key(storage_key)
        try:
            if not path.exists() or path.is_symlink() or not path.is_file():
                raise NotFound(f"Document storage key {storage_key!r} was not found.")
            path.unlink()
            _fsync_directory(path.parent)
        except NotFound:
            raise
        except OSError as error:
            raise StorageUnavailable(
                f"Document storage is unavailable at {path}.", path=path
            ) from error

    def stream(self, storage_key: str, *, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
        """Return a verified incremental plaintext iterator.

        The existence check happens before returning the generator.  A caller
        therefore receives ``NotFound`` for an absent object immediately,
        rather than receiving an iterator that looks like an empty document.
        """
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool):
            raise TypeError("Document stream chunk_size must be an integer.")
        if not 1 <= chunk_size <= _MAX_FRAME_BYTES:
            raise ValueError(
                f"Document stream chunk_size must be between 1 and {_MAX_FRAME_BYTES} bytes."
            )
        path = self._path_for_key(storage_key)
        try:
            if not path.exists() or path.is_symlink() or not path.is_file():
                raise NotFound(f"Document storage key {storage_key!r} was not found.")
            handle = path.open("rb")
        except NotFound:
            raise
        except FileNotFoundError as error:
            raise NotFound(f"Document storage key {storage_key!r} was not found.") from error
        except OSError as error:
            raise StorageUnavailable(
                f"Document storage is unavailable at {path}.", path=path
            ) from error
        return self._iter_file(handle, path, storage_key, chunk_size)

    def path_for(self, storage_key: str) -> Path:
        """Return the on-disk path for diagnostics and integrity checks."""
        return self._path_for_key(storage_key)

    def _iter_file(
        self,
        handle: BinaryIO,
        path: Path,
        storage_key: str,
        chunk_size: int,
    ) -> Iterator[bytes]:
        expected_hash = _hash_from_key(storage_key)
        digest = hashlib.sha256()
        total = 0
        try:
            header = _read_exact(handle, _HEADER.size, path)
            if len(header) != _HEADER.size:
                raise StorageUnavailable("Stored document envelope is truncated.", path=path)
            magic, expected_digest, expected_size = _HEADER.unpack(header)
            if magic != _MAGIC or expected_digest.hex() != expected_hash:
                raise StorageUnavailable("Stored document envelope failed validation.", path=path)

            while True:
                length_bytes = handle.read(_FRAME_LENGTH.size)
                if length_bytes == b"":
                    break
                if len(length_bytes) != _FRAME_LENGTH.size:
                    raise StorageUnavailable(
                        "Stored document frame header is truncated.", path=path
                    )
                (frame_length,) = _FRAME_LENGTH.unpack(length_bytes)
                if not 1 <= frame_length <= _MAX_FRAME_BYTES:
                    raise StorageUnavailable("Stored document frame length is invalid.", path=path)
                encrypted = _read_exact(handle, frame_length, path)
                if len(encrypted) != frame_length:
                    raise StorageUnavailable("Stored document frame is truncated.", path=path)
                try:
                    encoded_plaintext = self.encryptor.decrypt(encrypted.decode("ascii"))
                    if encoded_plaintext is None:
                        raise ValueError("empty encrypted document frame")
                    plaintext_bytes = base64.b64decode(encoded_plaintext, validate=True)
                except (
                    UnicodeDecodeError,
                    ExternalServiceError,
                    ValueError,
                    binascii.Error,
                ) as error:
                    raise StorageUnavailable(
                        "Stored document frame failed authentication.", path=path
                    ) from error
                if not plaintext_bytes:
                    raise StorageUnavailable("Stored document frame was empty.", path=path)
                digest.update(plaintext_bytes)
                total += len(plaintext_bytes)
                for offset in range(0, len(plaintext_bytes), chunk_size):
                    yield plaintext_bytes[offset : offset + chunk_size]

            if total != expected_size or digest.digest() != expected_digest:
                raise StorageUnavailable("Stored document integrity check failed.", path=path)
        except OSError as error:
            raise StorageUnavailable(
                f"Stored document could not be read at {path}.", path=path
            ) from error
        finally:
            handle.close()

    def _is_valid_existing(self, path: Path, storage_key: str) -> bool:
        """Validate a pre-existing blob before deduplicating into it."""
        try:
            handle = path.open("rb")
        except OSError as error:
            raise StorageUnavailable(
                f"Document storage is unavailable at {path}.", path=path
            ) from error
        try:
            for _ in self._iter_file(handle, path, storage_key, self.chunk_size):
                continue
        except StorageUnavailable:
            return False
        return True

    def _path_for_key(self, storage_key: str) -> Path:
        digest = _hash_from_key(storage_key)
        return self.root / _STORAGE_PREFIX / digest[:2] / digest[2:4] / digest

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        try:
            if path.exists():
                if path.is_symlink() or not path.is_dir():
                    raise StorageUnavailable(
                        f"Document storage path is not a directory: {path}.", path=path
                    )
            else:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.is_symlink() or not path.is_dir():
                    raise StorageUnavailable(
                        f"Document storage path is not a directory: {path}.", path=path
                    )
        except StorageUnavailable:
            raise
        except OSError as error:
            raise StorageUnavailable(
                f"Document storage is unavailable at {path}.", path=path
            ) from error


def _binary_source(content: bytes | bytearray | memoryview | BinaryIO) -> BinaryIO:
    if isinstance(content, bytes | bytearray | memoryview):
        from io import BytesIO

        return BytesIO(bytes(content))
    if not callable(getattr(content, "read", None)):
        raise TypeError("Document content must be bytes or a binary stream.")
    return content


def _validate_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _HASH_HEX_LENGTH
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Document content hashes must be lowercase SHA-256 hexadecimal values.")
    return value


def _hash_from_key(storage_key: str) -> str:
    if not isinstance(storage_key, str) or not storage_key.startswith(f"{_STORAGE_PREFIX}/"):
        raise NotFound(f"Document storage key {storage_key!r} was not found.")
    prefix, separator, digest = storage_key.partition("/")
    if prefix != _STORAGE_PREFIX or not separator:
        raise NotFound(f"Document storage key {storage_key!r} was not found.")
    try:
        return _validate_hash(digest)
    except ValueError as error:
        raise NotFound(f"Document storage key {storage_key!r} was not found.") from error


def _read_exact(handle: BinaryIO, count: int, path: Path) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = handle.read(remaining)
        if not isinstance(chunk, bytes):
            raise StorageUnavailable("Stored document could not be read.", path=path)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory durability on platforms that expose it."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


# Both spellings occur in deployment documentation; keep one implementation.
FilesystemDocumentStore = FileSystemDocumentStore
EncryptedFileSystemDocumentStore = FileSystemDocumentStore
EncryptedFilesystemDocumentStore = FileSystemDocumentStore
LocalDocumentStore = FileSystemDocumentStore


__all__ = [
    "EncryptedFileSystemDocumentStore",
    "EncryptedFilesystemDocumentStore",
    "FileSystemDocumentStore",
    "FilesystemDocumentStore",
    "LocalDocumentStore",
    "StorageUnavailable",
]
