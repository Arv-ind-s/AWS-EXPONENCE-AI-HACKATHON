"""Borrower tables: `plan.md §5.2`'s `borrower`, `borrower_group`,
`related_party` and `borrower_contact`.

`cin_enc` and `pan_enc` are field-encrypted (`T-017` supplies the type;
this module declares only the columns against its interface — opaque text
until then, exactly as `identity.AppUser.mfa_secret_enc` already does).
Uniqueness and lookup on CIN cannot wait for that: `cin_fingerprint` is a
deterministic HMAC of the CIN (also `T-017`'s to compute), stored
alongside the ciphertext, so a duplicate borrower is caught and an
existing record can be looked up without ever decrypting anything. It is
unique only **among active borrowers** — a deactivated borrower's
fingerprint does not go on blocking a corrected re-entry, and SQL's own
NULL semantics mean a borrower with no CIN on file never collides with
another one.

`related_party` and `borrower_contact` are `spec §16.2`'s **identifying
class**: every name, email, phone number and identifier they carry is
field-encrypted the same way.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from covenant_radar.db.base import Base, StandardColumns, VersionedColumns
from covenant_radar.db.models.identity import UserAttributedColumns
from covenant_radar.db.types import GUID

_REFERENCE_MAX_LENGTH = 20
_LEGAL_NAME_MAX_LENGTH = 300
_INDUSTRY_CODE_MAX_LENGTH = 20
_CONSTITUTION_MAX_LENGTH = 50
_FINGERPRINT_MAX_LENGTH = 128
_GROUP_NAME_MAX_LENGTH = 300
_PARTY_TYPE_MAX_LENGTH = 20
_PARTY_ROLE_MAX_LENGTH = 200
_DESIGNATION_MAX_LENGTH = 100

_PARTY_TYPES = ("promoter", "guarantor", "director", "signatory", "related_entity")


class BorrowerGroup(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A group of commonly-controlled borrowers, for concentration and
    related-party analysis across the whole group rather than one entity
    at a time."""

    __tablename__ = "borrower_group"

    name: Mapped[str] = mapped_column(String(_GROUP_NAME_MAX_LENGTH), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("borrower_group.id", ondelete="RESTRICT"), nullable=True
    )


class Borrower(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A legal entity with credit facilities.

    Deactivated, never deleted: `is_active=False` is how a closed
    relationship is recorded, and `reference` stays exactly as it was —
    nothing about a deactivated borrower's identity is ever freed for
    reuse except `cin_fingerprint`'s claim on uniqueness.
    """

    __tablename__ = "borrower"
    __table_args__ = (
        Index(
            "uq_borrower_cin_fingerprint_active",
            "cin_fingerprint",
            unique=True,
            sqlite_where=text("is_active"),
            postgresql_where=text("is_active"),
        ),
    )

    reference: Mapped[str] = mapped_column(
        String(_REFERENCE_MAX_LENGTH), nullable=False, unique=True
    )
    legal_name: Mapped[str] = mapped_column(String(_LEGAL_NAME_MAX_LENGTH), nullable=False)
    cin_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    pan_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    cin_fingerprint: Mapped[str | None] = mapped_column(
        String(_FINGERPRINT_MAX_LENGTH), nullable=True
    )
    industry_code: Mapped[str | None] = mapped_column(
        String(_INDUSTRY_CODE_MAX_LENGTH),
        ForeignKey("industry_reference.code", ondelete="RESTRICT"),
        nullable=True,
    )
    group_id: Mapped[UUID | None] = mapped_column(
        GUID, ForeignKey("borrower_group.id", ondelete="RESTRICT"), nullable=True
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("portfolio.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    constitution: Mapped[str | None] = mapped_column(
        String(_CONSTITUTION_MAX_LENGTH), nullable=True
    )
    incorporation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RelatedParty(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A promoter, guarantor, director, signatory or related entity tied
    to a borrower. **Identifying class**: `name_enc` and `identifier_enc`
    are field-encrypted.

    `effective_from` and `effective_to` are both nullable; the convention,
    documented here rather than on a separate flag that could fall out of
    sync, is that a row with neither set is currently effective.
    """

    __tablename__ = "related_party"
    __table_args__ = (
        CheckConstraint(
            "party_type IN (" + ", ".join(f"'{party_type}'" for party_type in _PARTY_TYPES) + ")",
            name="party_type_valid",
        ),
    )

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="CASCADE"), nullable=False, index=True
    )
    party_type: Mapped[str] = mapped_column(String(_PARTY_TYPE_MAX_LENGTH), nullable=False)
    name_enc: Mapped[str] = mapped_column(Text, nullable=False)
    identifier_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str | None] = mapped_column(String(_PARTY_ROLE_MAX_LENGTH), nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class BorrowerContact(Base, UserAttributedColumns, StandardColumns, VersionedColumns):
    """A contact person at a borrower. **Identifying class**: `name_enc`,
    `email_enc` and `phone_enc` are field-encrypted."""

    __tablename__ = "borrower_contact"

    borrower_id: Mapped[UUID] = mapped_column(
        GUID, ForeignKey("borrower.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name_enc: Mapped[str] = mapped_column(Text, nullable=False)
    email_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    designation: Mapped[str | None] = mapped_column(String(_DESIGNATION_MAX_LENGTH), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
