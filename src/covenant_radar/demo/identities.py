"""Safe v1-to-v2 identity upgrade for the reserved reference portfolio."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from random import Random
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.db.models.borrower import Borrower, BorrowerContact, BorrowerGroup
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.master_data import MasterDataService
from evaluation.reference_portfolio.generator import (
    DEFAULT_BORROWER_COUNT,
    DEFAULT_SEED,
    build_cin,
    build_pan,
)
from evaluation.reference_portfolio.names import (
    build_contact_email,
    build_group_name,
    build_legacy_legal_name,
    legal_name_v2,
    validate_v2_legal_names,
)

DEMO_PORTFOLIO_CODE: Final[str] = "REF-PORTFOLIO"
IDENTITY_VERSION: Final[str] = "company-identities-v2"
_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^B-(\d{6})$")
_LEGACY_GROUP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^Indian Commercial Group (\d{4}) Private Limited$"
)


@dataclass(frozen=True, slots=True)
class IdentityUpgradeReport:
    """Non-identifying summary of one identity upgrade."""

    borrowers_seen: int
    borrowers_renamed: int
    protected_borrower_names: tuple[str, ...]
    groups_renamed: int
    protected_group_names: int
    contacts_updated: int
    identity_hash: str


class _AuditWriter:
    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=request_id,
        )


def upgrade_reference_identities(
    session: Session,
    *,
    system_actor_id: UUID,
    clock: Clock | None = None,
) -> IdentityUpgradeReport:
    """Upgrade only exact v1 identities and preserve every manual edit.

    The caller owns the surrounding transaction.  ``MasterDataService``
    performs each borrower update with optimistic concurrency and appends its
    normal audit event.  Group and synthetic contact changes are summarized in
    one additional portfolio audit event because no public group/contact edit
    service exists.
    """

    if not isinstance(session, Session):
        raise TypeError("upgrade_reference_identities requires a SQLAlchemy Session.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("system_actor_id must be a UUID.")
    portfolio = session.scalar(select(Portfolio).where(Portfolio.code == DEMO_PORTFOLIO_CODE))
    if portfolio is None:
        raise ValueError(
            "The reference portfolio is missing. Run `radarctl seed --reference-portfolio` first."
        )

    active_clock = clock or SystemClock()
    now = active_clock.now()
    request_id = "demo-id-" + new_request_id()[:31]
    audit = _AuditWriter(
        AuditRecorder(AuditRepository(session), clock=active_clock, request_id=request_id)
    )
    principal = Principal.user(system_actor_id, (Permission.CORRECT_SOURCE_DATA,))
    scope = Scope(principal_id=system_actor_id, descendant_paths=(portfolio.path,))
    master_data = MasterDataService(
        session,
        audit=audit,
        clock=active_clock,
        request_id=request_id,
    )

    expected_v1 = _legacy_name_map(DEFAULT_BORROWER_COUNT)
    expected_v2 = tuple(legal_name_v2(index) for index in range(1, DEFAULT_BORROWER_COUNT + 1))
    validate_v2_legal_names(expected_v2)
    borrowers = list(
        session.scalars(
            select(Borrower)
            .where(Borrower.portfolio_id == portfolio.id)
            .order_by(Borrower.reference)
        )
    )
    protected: list[str] = []
    renamed = 0
    contacts_updated = 0
    for borrower in borrowers:
        sequence = _reference_sequence(borrower.reference)
        if sequence is None or sequence > DEFAULT_BORROWER_COUNT:
            protected.append(borrower.reference)
            continue
        target = legal_name_v2(sequence)
        legacy = expected_v1[sequence]
        if borrower.legal_name not in {legacy, target}:
            protected.append(borrower.reference)
        elif borrower.legal_name == legacy:
            master_data.update_borrower(
                principal,
                borrower.reference,
                expected_version=borrower.version,
                scope=scope,
                legal_name=target,
            )
            renamed += 1

        legacy_email = f"finance{sequence:06d}@reference.invalid"
        target_email = build_contact_email(sequence)
        contacts = session.scalars(
            select(BorrowerContact).where(BorrowerContact.borrower_id == borrower.id)
        ).all()
        for contact in contacts:
            if contact.email_enc == legacy_email:
                contact.email_enc = target_email
                contact.updated_at = now
                contact.updated_by_id = system_actor_id
                contact.request_id = request_id
                contact.version += 1
                contacts_updated += 1

    groups_renamed = 0
    protected_groups = 0
    groups = session.scalars(select(BorrowerGroup).order_by(BorrowerGroup.id)).all()
    for group in groups:
        legacy_match = _LEGACY_GROUP_PATTERN.fullmatch(group.name)
        if legacy_match is not None:
            sequence = int(legacy_match.group(1))
            group.name = build_group_name(sequence)
            group.updated_at = now
            group.updated_by_id = system_actor_id
            group.request_id = request_id
            group.version += 1
            groups_renamed += 1
            continue
        if not _is_v2_group_name(group.name):
            protected_groups += 1

    session.flush()
    digest = _identity_hash(expected_v2)
    audit.record(
        "master_data_reference_identities_upgraded",
        ("portfolio", portfolio.id),
        {
            "action": "reference_identities_upgraded",
            "identity_version": IDENTITY_VERSION,
            "borrowers_seen": len(borrowers),
            "borrowers_renamed": renamed,
            "protected_borrower_count": len(protected),
            "groups_renamed": groups_renamed,
            "protected_group_count": protected_groups,
            "contacts_updated": contacts_updated,
            "identity_hash": digest,
        },
        actor=system_actor_id,
        request_id=request_id,
    )
    return IdentityUpgradeReport(
        borrowers_seen=len(borrowers),
        borrowers_renamed=renamed,
        protected_borrower_names=tuple(protected),
        groups_renamed=groups_renamed,
        protected_group_names=protected_groups,
        contacts_updated=contacts_updated,
        identity_hash=digest,
    )


def _legacy_name_map(count: int) -> dict[int, str]:
    """Replay only the name/CIN/PAN draws made by the v1 generator."""

    random_source = Random(DEFAULT_SEED)
    group_count = max(1, (count + 9) // 10)
    for _ in range(group_count):
        random_source.random()
    result: dict[int, str] = {}
    for sequence in range(1, count + 1):
        result[sequence] = build_legacy_legal_name(random_source, sequence)
        build_cin(sequence, random_source)
        build_pan(sequence, random_source)
    return result


def _reference_sequence(reference: str) -> int | None:
    match = _REFERENCE_PATTERN.fullmatch(reference)
    return int(match.group(1)) if match is not None else None


def _is_v2_group_name(name: str) -> bool:
    return any(name == build_group_name(sequence) for sequence in range(1, 501))


def _identity_hash(names: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


__all__ = [
    "DEMO_PORTFOLIO_CODE",
    "IDENTITY_VERSION",
    "IdentityUpgradeReport",
    "upgrade_reference_identities",
]
