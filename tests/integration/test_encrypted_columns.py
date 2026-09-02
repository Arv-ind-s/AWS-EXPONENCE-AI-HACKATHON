"""Database-facing proofs for encrypted and fingerprint column types."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, select

from covenant_radar.db.types import EncryptedText, FingerprintType
from covenant_radar.security.crypto import (
    EncryptedField,
    FieldEncryptor,
    HMACFingerprinter,
    ResumableRotation,
)

_KEY = b"F" * 32
_OLD_KEY = b"O" * 32
_FINGERPRINT_KEY = b"P" * 32


def test_raw_dump_contains_no_plaintext_personal_value() -> None:
    encryptor = FieldEncryptor({"v1": _KEY}, "v1")
    expected_fingerprint = HMACFingerprinter(_FINGERPRINT_KEY).fingerprint("U12345MH2000PLC000001")
    metadata = MetaData()
    table = Table(
        "encrypted_fixture",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("personal_value", EncryptedText(encryptor), nullable=False),
        Column(
            "cin_fingerprint",
            FingerprintType(HMACFingerprinter(_FINGERPRINT_KEY)),
            nullable=False,
        ),
    )
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            insert(table).values(
                id=1,
                personal_value="Promoter name: Rhea Holdings",
                cin_fingerprint="U12345MH2000PLC000001",
            )
        )
        raw = connection.exec_driver_sql(
            "SELECT personal_value, cin_fingerprint FROM encrypted_fixture"
        ).one()
        loaded = connection.execute(select(table)).one()

    assert "Promoter name: Rhea Holdings" not in raw.personal_value
    assert raw.personal_value.startswith("cr1.")
    assert raw.cin_fingerprint == expected_fingerprint
    assert loaded.personal_value == "Promoter name: Rhea Holdings"
    assert loaded.cin_fingerprint == expected_fingerprint


@dataclass
class _Row:
    id: int
    personal_value: str


class _Checkpoint:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def load(self, job_name: str) -> str | None:
        return self.values.get(job_name)

    def save(self, job_name: str, cursor: str) -> None:
        self.values[job_name] = cursor


def test_rotation_is_resumable() -> None:
    old_encryptor = FieldEncryptor({"old": _OLD_KEY}, "old")
    current_encryptor = FieldEncryptor({"old": _OLD_KEY, "current": _KEY}, "current")
    rows = [
        _Row(index, old_encryptor.encrypt(f"Personal value {index}") or "") for index in range(1, 5)
    ]
    checkpoint = _Checkpoint()

    def fetch(cursor: str | None, batch_size: int) -> tuple[_Row, ...]:
        start = int(cursor) if cursor is not None else 0
        return tuple(row for row in rows if row.id > start)[:batch_size]

    def commit(batch: tuple[_Row, ...] | list[_Row]) -> None:
        assert all(row.personal_value.startswith("cr1.") for row in batch)

    first_run = ResumableRotation(
        job_name="encrypted-fixture",
        fetch_batch=fetch,
        commit_batch=commit,
        row_id=lambda row: row.id,
        fields=(EncryptedField.attribute("personal_value"),),
        encryptor=current_encryptor,
        checkpoint_store=checkpoint,
        batch_size=2,
    ).run(max_batches=1)

    assert not first_run.complete
    assert first_run.rotated == 2
    assert checkpoint.values["encrypted-fixture"] == "2"
    assert all(current_encryptor.is_current(row.personal_value) for row in rows[:2])
    assert not current_encryptor.is_current(rows[2].personal_value)

    second_run = ResumableRotation(
        job_name="encrypted-fixture",
        fetch_batch=fetch,
        commit_batch=commit,
        row_id=lambda row: row.id,
        fields=(EncryptedField.attribute("personal_value"),),
        encryptor=current_encryptor,
        checkpoint_store=checkpoint,
        batch_size=2,
    ).run()

    assert second_run.complete
    assert second_run.rotated == 2
    assert all(current_encryptor.is_current(row.personal_value) for row in rows)
