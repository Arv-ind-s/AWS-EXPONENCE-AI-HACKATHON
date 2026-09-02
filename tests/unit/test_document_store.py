"""Unit tests for encrypted, content-addressed document storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from covenant_radar.core.errors import NotFound
from covenant_radar.documents.store import FileSystemDocumentStore
from covenant_radar.security.crypto import FieldEncryptor

pytestmark = pytest.mark.unit


def _store(root: Path) -> FileSystemDocumentStore:
    return FileSystemDocumentStore(
        root,
        encryptor=FieldEncryptor({"documents-test": b"D" * 32}, "documents-test"),
        chunk_size=32,
    )


def test_content_addressed_key(tmp_path: Path) -> None:
    store = _store(tmp_path / "documents")
    content = b"%PDF-1.7\ncontent-addressed"

    first = store.put(content)
    second = store.put(content)

    assert first == second
    assert first.startswith("sha256/")
    assert first.rsplit("/", 1)[-1] in first
    assert len(list((tmp_path / "documents" / "sha256").rglob("*"))) == 3


def test_encrypted_at_rest(tmp_path: Path) -> None:
    store = _store(tmp_path / "documents")
    content = b"%PDF-1.7\nprivate sanction letter content"
    key = store.put(content)

    on_disk = store.path_for(key).read_bytes()

    assert content not in on_disk
    assert store.get(key) == content


def test_missing_key_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path / "documents")

    with pytest.raises(NotFound):
        store.get("sha256/" + "0" * 64)
    with pytest.raises(NotFound):
        list(store.stream("sha256/" + "0" * 64))


def test_streaming_does_not_load_whole_file(tmp_path: Path) -> None:
    store = _store(tmp_path / "documents")
    content = b"%PDF-1.7\n" + bytes(range(256)) * 10
    key = store.put(content)

    chunks = tuple(store.stream(key, chunk_size=17))

    assert len(chunks) > 10
    assert all(0 < len(chunk) <= 32 for chunk in chunks)
    assert b"".join(chunks) == content
