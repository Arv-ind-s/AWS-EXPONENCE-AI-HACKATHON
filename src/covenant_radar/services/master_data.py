"""Master-data use cases for borrowers, facilities, and portfolios.

The service is deliberately persistence-aware at this boundary: it coordinates
the scoped repositories, optimistic-concurrency checks, effective dating and
the audit port in one caller-owned transaction.  It never opens or commits a
transaction itself; the HTTP/job adapter supplies the unit of work described
by ``C-57``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import (
    AuthorizationError,
    Conflict,
    ExternalServiceError,
    NotFound,
    ValidationError,
)
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.facility import (
    CURRENT_STATUS,
    FacilityBookRow,
    FacilityListing,
    FacilityRepository,
)
from covenant_radar.db.repositories.portfolio import PortfolioRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.crypto import FieldEncryptor, HMACFingerprinter
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind, authorize


class AuditWriter(Protocol):
    """The append-only audit port from contract C-60."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the caller's current transaction."""


MASTER_DATA_READ_PERMISSION = Permission.VIEW_BORROWER
MASTER_DATA_WRITE_PERMISSION = Permission.CORRECT_SOURCE_DATA
type MasterDataEntity = Borrower | Facility | Portfolio


@dataclass(frozen=True, slots=True)
class RevealedIdentity:
    """The decrypted result of one privileged, purpose-logged identity read.

    Never constructed except by :meth:`MasterDataService.reveal_identity`,
    and never itself passed to the audit port: the decrypted values must
    stop here, not become a second, unencrypted copy in the audit trail.
    """

    borrower_id: UUID
    reference: str
    cin: str | None
    pan: str | None


class DuplicateCINConflict(Conflict):
    """A duplicate identity conflict that can safely offer an in-scope row."""

    def __init__(self, existing_reference: str) -> None:
        super().__init__(
            f"The CIN already belongs to borrower {existing_reference}; "
            "use the existing borrower instead.",
            field="borrower.cin",
        )
        self.existing_reference = existing_reference


class MasterDataService:
    """Coordinate all master-data state transitions.

    ``session`` must belong to the current unit of work.  Keeping that
    dependency explicit makes it impossible for a repository read and its
    corresponding write/audit event to drift into different transactions.
    """

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        encryptor: FieldEncryptor | None = None,
        fingerprinter: HMACFingerprinter | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("MasterDataService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("MasterDataService requires an append-only audit writer.")
        if encryptor is not None and not callable(getattr(encryptor, "encrypt", None)):
            raise TypeError("MasterDataService encryptor must expose encrypt().")
        if fingerprinter is not None and not callable(getattr(fingerprinter, "fingerprint", None)):
            raise TypeError("MasterDataService fingerprinter must expose fingerprint().")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("MasterDataService scope_resolver must be callable.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Master-data request_id must be between 1 and 40 characters.")
        self.encryptor = encryptor
        self.fingerprinter = fingerprinter
        self.scope_resolver = scope_resolver
        self.borrowers = BorrowerRepository(session, audit=audit)
        self.facilities = FacilityRepository(session, audit=audit)
        self.portfolios = PortfolioRepository(session, audit=audit)

    # ---- read use cases -------------------------------------------------

    def list_borrowers(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        active_only: bool | None = None,
        portfolio_id: UUID | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Borrower]:
        resolved_scope = self._read_context(principal, scope)
        return self.borrowers.ordered(
            scope=resolved_scope,
            active_only=active_only,
            portfolio_id=portfolio_id,
            search=search,
            limit=limit,
            offset=offset,
        )

    def get_borrower_by_id(
        self, principal: Principal, borrower_id: UUID, *, scope: Scope | None = None
    ) -> Borrower:
        """Return one in-scope borrower by id, for a record that only holds one."""
        resolved_scope = self._read_context(principal, scope)
        borrower = self.borrowers.get(borrower_id, scope=resolved_scope)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found within the current scope.")
        return borrower

    def get_borrower(
        self, principal: Principal, reference: str, *, scope: Scope | None = None
    ) -> Borrower:
        resolved_scope = self._read_context(principal, scope)
        borrower = self.borrowers.by_reference(
            _clean_reference(reference, "borrower.reference"), scope=resolved_scope
        )
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        return borrower

    def list_facilities(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        current_only: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Facility]:
        resolved_scope = self._read_context(principal, scope)
        return self.facilities.ordered(
            scope=resolved_scope,
            current_only=current_only,
            limit=limit,
            offset=offset,
        )

    def list_facility_listings(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        status: str = CURRENT_STATUS,
        search: str | None = None,
        facility_type: str | None = None,
        currency: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[FacilityListing]:
        """List facilities for a browsing screen, each named by its borrower."""
        resolved_scope = self._read_context(principal, scope)
        return self.facilities.ordered_with_borrower(
            scope=resolved_scope,
            status=status,
            search=search,
            facility_type=facility_type,
            currency=currency,
            limit=limit,
            offset=offset,
        )

    def count_facilities(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        status: str = CURRENT_STATUS,
        search: str | None = None,
        facility_type: str | None = None,
        currency: str | None = None,
    ) -> int:
        """Count the facilities one filtered list screen would page through."""
        resolved_scope = self._read_context(principal, scope)
        return self.facilities.count(
            scope=resolved_scope,
            status=status,
            search=search,
            facility_type=facility_type,
            currency=currency,
        )

    def facility_filter_values(
        self, principal: Principal, *, scope: Scope | None = None
    ) -> dict[str, Sequence[str]]:
        """Return the facility type and currency choices present in scope.

        The filters offer only values the reader can actually reach, so an
        empty result set is always the reader's own filter combination rather
        than a choice that never existed in their portfolios.
        """
        resolved_scope = self._read_context(principal, scope)
        return {
            "facility_type": self.facilities.distinct_values("facility_type", scope=resolved_scope),
            "currency": self.facilities.distinct_values("currency", scope=resolved_scope),
        }

    def facility_book(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        current_only: bool = True,
        limit: int | None = None,
    ) -> Sequence[FacilityBookRow]:
        """Return every in-scope facility as summary input for the book view."""
        resolved_scope = self._read_context(principal, scope)
        return self.facilities.book_rows(
            scope=resolved_scope, current_only=current_only, limit=limit
        )

    def facility_revisions(
        self, principal: Principal, reference: str, *, scope: Scope | None = None
    ) -> Sequence[Facility]:
        """Return one facility's effective-dated versions, oldest first."""
        resolved_scope = self._read_context(principal, scope)
        facility = self.get_facility(principal, reference, scope=resolved_scope)
        return self.facilities.revision_chain(facility, scope=resolved_scope)

    def list_facilities_for_borrower(
        self,
        principal: Principal,
        borrower_id: UUID,
        *,
        scope: Scope | None = None,
        current_only: bool = True,
    ) -> Sequence[Facility]:
        """Return facilities for a borrower after validating borrower scope."""
        resolved_scope = self._read_context(principal, scope)
        borrower = self.borrowers.get(borrower_id, scope=resolved_scope)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found within the current scope.")
        return self.facilities.for_borrower(
            borrower.id, scope=resolved_scope, current_only=current_only
        )

    def get_facility(
        self, principal: Principal, reference: str, *, scope: Scope | None = None
    ) -> Facility:
        resolved_scope = self._read_context(principal, scope)
        facility = self.facilities.by_reference(
            _clean_reference(reference, "facility.reference"), scope=resolved_scope
        )
        if facility is None:
            raise NotFound(f"Facility {reference!r} was not found within the current scope.")
        return facility

    def get_facility_as_of(
        self,
        principal: Principal,
        *,
        borrower_reference: str,
        as_of: date,
        scope: Scope | None = None,
    ) -> Facility:
        """Read the facility version effective on an historical date."""
        if not isinstance(as_of, date):
            raise ValidationError("as_of must be a date.", field="facility.as_of")
        resolved_scope = self._read_context(principal, scope)
        borrower = self.borrowers.by_reference(
            _clean_reference(borrower_reference, "borrower.reference"), scope=resolved_scope
        )
        if borrower is None:
            raise NotFound(
                f"Borrower {borrower_reference!r} was not found within the current scope."
            )
        facility = self.facilities.as_of(borrower.id, as_of, scope=resolved_scope)
        if facility is None:
            raise NotFound(
                f"No facility for borrower {borrower_reference!r} was effective on {as_of}."
            )
        return facility

    def list_portfolios(
        self,
        principal: Principal,
        *,
        scope: Scope | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Sequence[Portfolio]:
        resolved_scope = self._read_context(principal, scope)
        return self.portfolios.ordered(scope=resolved_scope, limit=limit, offset=offset)

    def get_portfolio(
        self, principal: Principal, portfolio_id: UUID, *, scope: Scope | None = None
    ) -> Portfolio:
        resolved_scope = self._read_context(principal, scope)
        portfolio = self.portfolios.by_id(portfolio_id, scope=resolved_scope)
        if portfolio is None:
            raise NotFound(f"Portfolio {portfolio_id} was not found within the current scope.")
        return portfolio

    # ---- privileged personal-data read -----------------------------------

    def reveal_identity(
        self,
        principal: Principal,
        reference: str,
        *,
        purpose: str,
        scope: Scope | None = None,
    ) -> RevealedIdentity:
        """Decrypt a borrower's protected CIN/PAN for one authorised read.

        Gated on ``READ_PERSONAL_DATA`` rather than the ordinary borrower
        read permission, because every other read in this service returns
        rows with these two fields still encrypted.  Every call — regardless
        of whether a value was actually present to decrypt — is recorded as
        an access event naming the caller's stated purpose; the decrypted
        values themselves are never passed to the audit port.
        """
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, Permission.READ_PERSONAL_DATA)
        resolved_scope = self._validated_scope(principal, scope)
        validated_purpose = _required_text(purpose, "purpose", maximum=200)
        borrower = self.borrowers.by_reference(
            _clean_reference(reference, "borrower.reference"), scope=resolved_scope
        )
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        if self.encryptor is None:
            raise ExternalServiceError(
                "Borrower identity fields cannot be revealed because field "
                "encryption is not configured."
            )
        cin = self.encryptor.decrypt(borrower.cin_enc) if borrower.cin_enc is not None else None
        pan = self.encryptor.decrypt(borrower.pan_enc) if borrower.pan_enc is not None else None
        self.audit.record(
            AuditEventType.MASTER_DATA_PERSONAL_DATA_ACCESSED.value,
            ("borrower", borrower.id),
            {
                "action": "personal_data_accessed",
                "reference": borrower.reference,
                "purpose": validated_purpose,
                "fields": [
                    name for name, value in (("cin", cin), ("pan", pan)) if value is not None
                ],
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        return RevealedIdentity(
            borrower_id=borrower.id,
            reference=borrower.reference,
            cin=cin,
            pan=pan,
        )

    # ---- borrower writes ------------------------------------------------

    def create_borrower(
        self,
        principal: Principal,
        *,
        reference: str,
        legal_name: str,
        portfolio_id: UUID,
        cin: str | None = None,
        pan: str | None = None,
        industry_code: str | None = None,
        group_id: UUID | None = None,
        constitution: str | None = None,
        incorporation_date: date | None = None,
        scope: Scope | None = None,
    ) -> Borrower:
        resolved_scope = self._write_context(principal, scope)
        reference = _clean_reference(reference, "borrower.reference", maximum=20)
        legal_name = _required_text(legal_name, "borrower.legal_name", maximum=300)
        portfolio = self._portfolio_in_scope(portfolio_id, resolved_scope)
        cin_enc, cin_fingerprint = self._protect_identity(cin, field="borrower.cin")
        pan_enc, _ = self._protect_identity(pan, field="borrower.pan", fingerprint=False)
        if cin_fingerprint is not None:
            duplicate = self.borrowers.find(
                scope=resolved_scope,
                cin_fingerprint=cin_fingerprint,
                is_active=True,
            )
            if duplicate is not None:
                raise _duplicate_cin(duplicate.reference)
        now = self._now()
        borrower = Borrower(
            id=new_id(),
            reference=reference,
            legal_name=legal_name,
            cin_enc=cin_enc,
            pan_enc=pan_enc,
            cin_fingerprint=cin_fingerprint,
            industry_code=_optional_text(industry_code, "borrower.industry_code", maximum=20),
            group_id=group_id,
            portfolio_id=portfolio.id,
            constitution=_optional_text(constitution, "borrower.constitution", maximum=50),
            incorporation_date=incorporation_date,
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by_id=self._attributed_id(principal),
            updated_by_id=self._attributed_id(principal),
            request_id=self.request_id,
        )
        self.borrowers.add(borrower)
        self._flush_or_conflict(
            f"Borrower reference {reference!r} or active CIN already exists.",
            duplicate_subject="borrower.reference",
        )
        self._audit(
            AuditEventType.MASTER_DATA_BORROWER_CREATED.value,
            borrower,
            {
                "action": "created",
                "reference": borrower.reference,
                "portfolio_id": str(borrower.portfolio_id),
                "cin_present": cin is not None,
                "pan_present": pan is not None,
            },
            principal,
        )
        return borrower

    def update_borrower(
        self,
        principal: Principal,
        reference: str,
        *,
        expected_version: int,
        scope: Scope | None = None,
        **changes: object,
    ) -> Borrower:
        resolved_scope = self._write_context(principal, scope)
        borrower = self.borrowers.by_reference(
            _clean_reference(reference, "borrower.reference"), scope=resolved_scope
        )
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        self._check_version(
            borrower,
            expected_version,
            resource="Borrower",
            requested_fields=tuple(sorted(changes)),
        )
        if not changes:
            raise ValidationError("At least one borrower field must be changed.", field="borrower")
        allowed = {
            "legal_name",
            "portfolio_id",
            "cin",
            "pan",
            "industry_code",
            "group_id",
            "constitution",
            "incorporation_date",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"Unsupported borrower field(s): {', '.join(sorted(unknown))}.",
                field="borrower",
            )
        changed_fields: list[str] = []
        if "portfolio_id" in changes:
            value = changes["portfolio_id"]
            if not isinstance(value, UUID):
                raise ValidationError("portfolio_id must be a UUID.", field="borrower.portfolio_id")
            target_portfolio = self._portfolio_in_scope(value, resolved_scope)
            if target_portfolio.id != borrower.portfolio_id:
                borrower.portfolio_id = target_portfolio.id
                changed_fields.append("portfolio_id")
        if "legal_name" in changes:
            value = _required_text(changes["legal_name"], "borrower.legal_name", maximum=300)
            if value != borrower.legal_name:
                borrower.legal_name = value
                changed_fields.append("legal_name")
        if "cin" in changes:
            value = changes["cin"]
            if value is not None and not isinstance(value, str):
                raise ValidationError("cin must be text or null.", field="borrower.cin")
            cin_enc, fingerprint = self._protect_identity(value, field="borrower.cin")
            if fingerprint != borrower.cin_fingerprint:
                duplicate = (
                    self.borrowers.find(
                        scope=resolved_scope,
                        cin_fingerprint=fingerprint,
                        is_active=True,
                    )
                    if fingerprint is not None
                    else None
                )
                if duplicate is not None and duplicate.id != borrower.id:
                    raise _duplicate_cin(duplicate.reference)
                borrower.cin_enc = cin_enc
                borrower.cin_fingerprint = fingerprint
                changed_fields.append("cin")
        if "pan" in changes:
            value = changes["pan"]
            if value is not None and not isinstance(value, str):
                raise ValidationError("pan must be text or null.", field="borrower.pan")
            pan_enc, _ = self._protect_identity(value, field="borrower.pan", fingerprint=False)
            if pan_enc != borrower.pan_enc:
                borrower.pan_enc = pan_enc
                changed_fields.append("pan")
        for field, maximum in (
            ("industry_code", 20),
            ("constitution", 50),
        ):
            if field in changes:
                value = _optional_text(changes[field], f"borrower.{field}", maximum=maximum)
                if getattr(borrower, field) != value:
                    setattr(borrower, field, value)
                    changed_fields.append(field)
        for field in ("group_id", "incorporation_date"):
            if field in changes:
                value = changes[field]
                if field == "group_id" and value is not None and not isinstance(value, UUID):
                    raise ValidationError(
                        "group_id must be a UUID or null.", field="borrower.group_id"
                    )
                if (
                    field == "incorporation_date"
                    and value is not None
                    and not isinstance(value, date)
                ):
                    raise ValidationError(
                        "incorporation_date must be a date or null.",
                        field="borrower.incorporation_date",
                    )
                if getattr(borrower, field) != value:
                    setattr(borrower, field, value)
                    changed_fields.append(field)
        if not changed_fields:
            return borrower
        self._touch(borrower, principal)
        self._flush_or_conflict("Borrower update conflicts with an existing active CIN.")
        self._audit(
            AuditEventType.MASTER_DATA_BORROWER_UPDATED.value,
            borrower,
            {
                "action": "updated",
                "reference": borrower.reference,
                "changed_fields": changed_fields,
            },
            principal,
        )
        return borrower

    def deactivate_borrower(
        self,
        principal: Principal,
        reference: str,
        *,
        expected_version: int,
        scope: Scope | None = None,
    ) -> Borrower:
        resolved_scope = self._write_context(principal, scope)
        borrower = self.get_borrower(principal, reference, scope=resolved_scope)
        self._check_version(
            borrower,
            expected_version,
            resource="Borrower",
            requested_fields=("is_active",),
        )
        if not borrower.is_active:
            return borrower
        live_facilities = self.facilities.live_for_borrower(borrower.id, scope=resolved_scope)
        if live_facilities:
            names = ", ".join(facility.reference for facility in live_facilities)
            raise Conflict(
                f"Borrower {borrower.reference} cannot be deactivated while live facilities "
                f"exist: {names}."
            )
        borrower.is_active = False
        self._touch(borrower, principal)
        self._audit(
            AuditEventType.MASTER_DATA_BORROWER_DEACTIVATED.value,
            borrower,
            {"action": "deactivated", "reference": borrower.reference},
            principal,
        )
        return borrower

    # ---- facility writes -----------------------------------------------

    def create_facility(
        self,
        principal: Principal,
        *,
        reference: str,
        borrower_id: UUID,
        facility_type: str,
        sanctioned_limit: Decimal,
        currency: str,
        sanction_date: date,
        effective_from: date,
        drawing_power: Decimal | None = None,
        outstanding: Decimal | None = None,
        security_type: str | None = None,
        pricing_bps: int | None = None,
        maturity_date: date | None = None,
        scope: Scope | None = None,
    ) -> Facility:
        resolved_scope = self._write_context(principal, scope)
        borrower = self.borrowers.get(borrower_id, scope=resolved_scope)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found within the current scope.")
        if not borrower.is_active:
            raise Conflict(
                f"Facility cannot be created for inactive borrower {borrower.reference}."
            )
        self._validate_facility_values(
            facility_type=facility_type,
            sanctioned_limit=sanctioned_limit,
            currency=currency,
            sanction_date=sanction_date,
            effective_from=effective_from,
            maturity_date=maturity_date,
            drawing_power=drawing_power,
            outstanding=outstanding,
            security_type=security_type,
            pricing_bps=pricing_bps,
        )
        reference = _clean_reference(reference, "facility.reference", maximum=24)
        now = self._now()
        facility = Facility(
            id=new_id(),
            reference=reference,
            borrower_id=borrower.id,
            facility_type=_required_text(facility_type, "facility.facility_type", maximum=50),
            sanctioned_limit=sanctioned_limit,
            currency=_currency(currency),
            drawing_power=drawing_power,
            outstanding=outstanding,
            security_type=_optional_text(security_type, "facility.security_type", maximum=100),
            pricing_bps=pricing_bps,
            sanction_date=sanction_date,
            maturity_date=maturity_date,
            effective_from=effective_from,
            effective_to=None,
            superseded_by_id=None,
            created_at=now,
            updated_at=now,
            created_by_id=self._attributed_id(principal),
            updated_by_id=self._attributed_id(principal),
            request_id=self.request_id,
        )
        self.facilities.add(facility)
        self._flush_or_conflict(f"Facility reference {reference!r} already exists.")
        self._audit(
            AuditEventType.MASTER_DATA_FACILITY_CREATED.value,
            facility,
            {
                "action": "created",
                "reference": facility.reference,
                "borrower_id": str(facility.borrower_id),
                "sanctioned_limit": str(facility.sanctioned_limit),
                "currency": facility.currency,
            },
            principal,
        )
        return facility

    def update_facility(
        self,
        principal: Principal,
        reference: str,
        *,
        expected_version: int,
        sanctioned_limit: Decimal | None = None,
        effective_from: date | None = None,
        new_reference: str | None = None,
        scope: Scope | None = None,
        **changes: object,
    ) -> Facility:
        resolved_scope = self._write_context(principal, scope)
        current = self.facilities.by_reference(
            _clean_reference(reference, "facility.reference"), scope=resolved_scope
        )
        if current is None:
            raise NotFound(f"Facility {reference!r} was not found within the current scope.")
        if current.effective_to is not None:
            raise Conflict(f"Facility {current.reference} is historical and cannot be edited.")
        locked = self.facilities.by_id_for_update(current.id, scope=resolved_scope)
        if locked is None:
            raise NotFound(f"Facility {reference!r} was not found within the current scope.")
        current = locked
        self._check_version(
            current,
            expected_version,
            resource="Facility",
            requested_fields=("sanctioned_limit", *sorted(changes))
            if sanctioned_limit is not None
            else tuple(sorted(changes)),
        )
        if sanctioned_limit is not None and sanctioned_limit != current.sanctioned_limit:
            successor_date = effective_from or self._now().date()
            overrides = dict(changes)
            overrides = self._normalise_facility_overrides(overrides)
            if new_reference is None:
                new_reference = _successor_reference(current.reference, current.version)
            new_reference = _clean_reference(new_reference, "facility.reference", maximum=24)
            self._validate_facility_values(
                facility_type=str(overrides.get("facility_type", current.facility_type)),
                sanctioned_limit=sanctioned_limit,
                currency=str(overrides.get("currency", current.currency)),
                sanction_date=overrides.get("sanction_date", current.sanction_date),
                effective_from=successor_date,
                maturity_date=overrides.get("maturity_date", current.maturity_date),
                drawing_power=overrides.get("drawing_power", current.drawing_power),
                outstanding=overrides.get("outstanding", current.outstanding),
                security_type=overrides.get("security_type", current.security_type),
                pricing_bps=overrides.get("pricing_bps", current.pricing_bps),
            )
            successor = Facility.supersede(
                current,
                reference=new_reference,
                effective_from=successor_date,
                created_at=self._now(),
                updated_at=self._now(),
                request_id=self.request_id,
                created_by_id=self._attributed_id(principal),
                updated_by_id=self._attributed_id(principal),
                sanctioned_limit=sanctioned_limit,
                **overrides,
            )
            self._touch(current, principal)
            self.facilities.add(successor)
            self._flush_or_conflict(
                f"Facility reference {new_reference!r} already exists; choose another reference."
            )
            self._audit(
                AuditEventType.MASTER_DATA_FACILITY_LIMIT_CHANGED.value,
                successor,
                {
                    "action": "effective_dated_update",
                    "predecessor_reference": current.reference,
                    "successor_reference": successor.reference,
                    "changed_fields": ["sanctioned_limit", *sorted(overrides)],
                    "effective_from": successor.effective_from.isoformat(),
                },
                principal,
            )
            return successor

        if effective_from is not None or new_reference is not None:
            raise ValidationError(
                "effective_from and new_reference are only valid with a changed sanctioned_limit.",
                field="facility.sanctioned_limit",
            )
        normalized = self._normalise_facility_overrides(changes)
        if not normalized:
            raise ValidationError("At least one facility field must be changed.", field="facility")
        changed_fields: list[str] = []
        for field, value in normalized.items():
            if getattr(current, field) != value:
                setattr(current, field, value)
                changed_fields.append(field)
        if not changed_fields:
            return current
        self._touch(current, principal)
        self._audit(
            AuditEventType.MASTER_DATA_FACILITY_UPDATED.value,
            current,
            {"action": "updated", "reference": current.reference, "changed_fields": changed_fields},
            principal,
        )
        return current

    def deactivate_facility(
        self,
        principal: Principal,
        reference: str,
        *,
        expected_version: int,
        effective_to: date | None = None,
        scope: Scope | None = None,
    ) -> Facility:
        resolved_scope = self._write_context(principal, scope)
        current = self.facilities.by_reference(
            _clean_reference(reference, "facility.reference"), scope=resolved_scope
        )
        if current is None:
            raise NotFound(f"Facility {reference!r} was not found within the current scope.")
        current = self.facilities.by_id_for_update(current.id, scope=resolved_scope) or current
        self._check_version(
            current,
            expected_version,
            resource="Facility",
            requested_fields=("effective_to",),
        )
        close_date = effective_to or self._now().date()
        if close_date <= current.effective_from:
            raise ValidationError(
                "effective_to must be after effective_from.", field="facility.effective_to"
            )
        if current.effective_to is not None:
            return current
        current.effective_to = close_date
        self._touch(current, principal)
        self._audit(
            AuditEventType.MASTER_DATA_FACILITY_DEACTIVATED.value,
            current,
            {
                "action": "deactivated",
                "reference": current.reference,
                "effective_to": close_date.isoformat(),
            },
            principal,
        )
        return current

    # ---- portfolio writes ----------------------------------------------

    def create_portfolio(
        self,
        principal: Principal,
        *,
        code: str,
        name: str,
        parent_id: UUID | None = None,
        branch_code: str | None = None,
        scope: Scope | None = None,
    ) -> Portfolio:
        resolved_scope = self._write_context(principal, scope)
        parent = None
        if parent_id is not None:
            parent = self.portfolios.by_id(parent_id, scope=resolved_scope)
            if parent is None:
                raise NotFound(
                    f"Parent portfolio {parent_id} was not found within the current scope."
                )
        code = _required_text(code, "portfolio.code", maximum=64)
        name = _required_text(name, "portfolio.name", maximum=200)
        branch_code = _optional_text(branch_code, "portfolio.branch_code", maximum=32)
        if parent is not None and self.portfolios.find(scope=resolved_scope, code=code) is not None:
            raise Conflict(f"Portfolio code {code!r} already exists within the current scope.")
        now = self._now()
        portfolio = Portfolio.create(
            code=code,
            name=name,
            parent=parent,
            branch_code=branch_code,
            created_at=now,
            updated_at=now,
            request_id=self.request_id,
            created_by_id=self._attributed_id(principal),
            updated_by_id=self._attributed_id(principal),
        )
        self.portfolios.add(portfolio)
        self._flush_or_conflict(f"Portfolio code {code!r} already exists.")
        self._audit(
            AuditEventType.MASTER_DATA_PORTFOLIO_CREATED.value,
            portfolio,
            {
                "action": "created",
                "code": portfolio.code,
                "parent_id": str(parent.id) if parent else None,
            },
            principal,
        )
        return portfolio

    def update_portfolio(
        self,
        principal: Principal,
        portfolio_id: UUID,
        *,
        expected_version: int,
        scope: Scope | None = None,
        **changes: object,
    ) -> Portfolio:
        resolved_scope = self._write_context(principal, scope)
        portfolio = self.portfolios.by_id(portfolio_id, scope=resolved_scope)
        if portfolio is None:
            raise NotFound(f"Portfolio {portfolio_id} was not found within the current scope.")
        portfolio = (
            self.portfolios.by_id_for_update(portfolio.id, scope=resolved_scope) or portfolio
        )
        self._check_version(
            portfolio,
            expected_version,
            resource="Portfolio",
            requested_fields=tuple(sorted(changes)),
        )
        allowed = {"code", "name", "branch_code", "parent_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"Unsupported portfolio field(s): {', '.join(sorted(unknown))}.", field="portfolio"
            )
        changed_fields: list[str] = []
        if "code" in changes:
            value = _required_text(changes["code"], "portfolio.code", maximum=64)
            if value != portfolio.code:
                portfolio.code = value
                changed_fields.append("code")
        if "name" in changes:
            value = _required_text(changes["name"], "portfolio.name", maximum=200)
            if value != portfolio.name:
                portfolio.name = value
                changed_fields.append("name")
        if "branch_code" in changes:
            branch_value = _optional_text(
                changes["branch_code"], "portfolio.branch_code", maximum=32
            )
            if branch_value != portfolio.branch_code:
                portfolio.branch_code = branch_value
                changed_fields.append("branch_code")
        if "parent_id" in changes:
            parent_value = changes["parent_id"]
            if parent_value is not None and not isinstance(parent_value, UUID):
                raise ValidationError(
                    "parent_id must be a UUID or null.", field="portfolio.parent_id"
                )
            if parent_value == portfolio.id:
                raise ValidationError(
                    "A portfolio cannot be its own parent.", field="portfolio.parent_id"
                )
            parent = None
            if parent_value is not None:
                parent = self.portfolios.by_id(parent_value, scope=resolved_scope)
                if parent is None:
                    raise NotFound(
                        f"Parent portfolio {parent_value} was not found within the current scope."
                    )
                if parent.path.startswith(portfolio.path):
                    raise ValidationError(
                        "A portfolio cannot be moved beneath its own descendant.",
                        field="portfolio.parent_id",
                    )
            if parent_value != portfolio.parent_id:
                if portfolio.path not in resolved_scope.descendant_paths:
                    raise AuthorizationError(
                        "Moving a portfolio requires descendant scope for the whole subtree."
                    )
                descendants = self.portfolios.descendants(portfolio, scope=resolved_scope)
                portfolio.move_to(parent, descendants=descendants)
                changed_fields.append("parent_id")
        if not changed_fields:
            return portfolio
        self._touch(portfolio, principal)
        self._audit(
            AuditEventType.MASTER_DATA_PORTFOLIO_UPDATED.value,
            portfolio,
            {
                "action": "updated",
                "portfolio_id": str(portfolio.id),
                "changed_fields": changed_fields,
            },
            principal,
        )
        return portfolio

    # ---- internal invariants -------------------------------------------

    def _read_context(self, principal: Principal, scope: Scope | None) -> Scope:
        self._require_principal(principal, MASTER_DATA_READ_PERMISSION)
        return self._validated_scope(principal, scope)

    def _write_context(self, principal: Principal, scope: Scope | None) -> Scope:
        self._require_principal(principal, MASTER_DATA_WRITE_PERMISSION)
        return self._validated_scope(principal, scope)

    def _validated_scope(self, principal: Principal, scope: Scope | None) -> Scope:
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
            if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
                raise AuthorizationError(
                    "The resolved scope does not belong to the authenticated principal."
                )
            return resolved
        if scope.principal_id != principal.id:
            raise AuthorizationError(
                "The supplied scope does not belong to the authenticated principal."
            )
        return scope

    @staticmethod
    def _require_principal(principal: Principal, permission: Permission) -> None:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)

    def _portfolio_in_scope(self, portfolio_id: UUID, scope: Scope) -> Portfolio:
        portfolio = self.portfolios.by_id(portfolio_id, scope=scope)
        if portfolio is None:
            raise NotFound(f"Portfolio {portfolio_id} was not found within the current scope.")
        return portfolio

    def _protect_identity(
        self,
        value: str | None,
        *,
        field: str,
        fingerprint: bool = True,
    ) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        clean = _required_text(value, field, maximum=300)
        if self.encryptor is None:
            raise ExternalServiceError(
                f"{field} cannot be stored because field encryption is not configured."
            )
        encrypted = self.encryptor.encrypt(clean)
        digest = (
            self.fingerprinter.fingerprint(clean) if fingerprint and self.fingerprinter else None
        )
        if fingerprint and digest is None:
            raise ExternalServiceError(
                f"{field} cannot be stored because fingerprinting is not configured."
            )
        return encrypted, digest

    def _normalise_facility_overrides(self, changes: Mapping[str, object]) -> dict[str, object]:
        allowed = {
            "facility_type",
            "currency",
            "drawing_power",
            "outstanding",
            "security_type",
            "pricing_bps",
            "sanction_date",
            "maturity_date",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"Unsupported facility field(s): {', '.join(sorted(unknown))}.", field="facility"
            )
        values = dict(changes)
        if "facility_type" in values:
            values["facility_type"] = _required_text(
                values["facility_type"], "facility.facility_type", maximum=50
            )
        if "currency" in values:
            values["currency"] = _currency(values["currency"])
        if "security_type" in values:
            values["security_type"] = _optional_text(
                values["security_type"], "facility.security_type", maximum=100
            )
        for field in ("drawing_power", "outstanding"):
            if (
                field in values
                and values[field] is not None
                and not isinstance(values[field], Decimal)
            ):
                raise ValidationError(
                    f"{field} must be a Decimal or null.", field=f"facility.{field}"
                )
        if "pricing_bps" in values:
            value = values["pricing_bps"]
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValidationError(
                    "pricing_bps must be a non-negative integer or null.",
                    field="facility.pricing_bps",
                )
        for field in ("sanction_date", "maturity_date"):
            if (
                field in values
                and values[field] is not None
                and not isinstance(values[field], date)
            ):
                raise ValidationError(f"{field} must be a date or null.", field=f"facility.{field}")
        return values

    @staticmethod
    def _validate_facility_values(**values: object) -> None:
        _required_text(values["facility_type"], "facility.facility_type", maximum=50)
        limit = values["sanctioned_limit"]
        if not isinstance(limit, Decimal) or limit <= Decimal("0"):
            raise ValidationError(
                "sanctioned_limit must be a positive Decimal.", field="facility.sanctioned_limit"
            )
        _currency(values["currency"])
        sanction_date = values["sanction_date"]
        effective_from = values["effective_from"]
        maturity_date = values["maturity_date"]
        if not isinstance(sanction_date, date):
            raise ValidationError("sanction_date must be a date.", field="facility.sanction_date")
        if not isinstance(effective_from, date):
            raise ValidationError("effective_from must be a date.", field="facility.effective_from")
        if effective_from < sanction_date:
            raise ValidationError(
                "effective_from cannot precede sanction_date.", field="facility.effective_from"
            )
        if maturity_date is not None and (
            not isinstance(maturity_date, date) or maturity_date < sanction_date
        ):
            raise ValidationError(
                "maturity_date cannot precede sanction_date.", field="facility.maturity_date"
            )
        for field in ("drawing_power", "outstanding"):
            value = values[field]
            if value is not None and (not isinstance(value, Decimal) or value < Decimal("0")):
                raise ValidationError(
                    f"{field} must be a non-negative Decimal or null.", field=f"facility.{field}"
                )
        pricing = values["pricing_bps"]
        if pricing is not None and (not isinstance(pricing, int) or pricing < 0):
            raise ValidationError(
                "pricing_bps must be a non-negative integer or null.", field="facility.pricing_bps"
            )
        _optional_text(values["security_type"], "facility.security_type", maximum=100)

    def _check_version(
        self,
        entity: object,
        expected_version: int,
        *,
        resource: str,
        requested_fields: Sequence[str] = (),
    ) -> None:
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError(
                "expected_version must be a positive integer.", field="expected_version"
            )
        current_version = getattr(entity, "version", None)
        if current_version != expected_version:
            reference = getattr(entity, "reference", str(getattr(entity, "id", "unknown")))
            changed_by = getattr(entity, "updated_by_id", None)
            actor = str(changed_by) if changed_by is not None else "an unknown actor"
            fields = ", ".join(requested_fields) if requested_fields else "the requested fields"
            raise Conflict(
                f"{resource} {reference} changed since version {expected_version} while "
                f"updating {fields}: "
                f"the current version is {current_version}, and the change was made by {actor}."
            )

    def _touch(self, entity: MasterDataEntity, principal: Principal) -> None:
        now = self._now()
        entity.updated_at = now
        entity.updated_by_id = self._attributed_id(principal)
        entity.version = entity.version + 1

    def _audit(
        self,
        event_type: str,
        entity: MasterDataEntity,
        payload: Mapping[str, object],
        principal: Principal,
    ) -> None:
        subject_type = type(entity).__name__.lower()
        self.audit.record(
            event_type,
            (subject_type, entity.id),
            dict(payload),
            actor=principal.id,
            request_id=self.request_id,
        )

    def _flush_or_conflict(self, message: str, *, duplicate_subject: str | None = None) -> None:
        try:
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as error:
            raise Conflict(message, field=duplicate_subject) from error

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Master-data clock must return an aware datetime.")
        return now.astimezone(UTC)

    @staticmethod
    def _attributed_id(principal: Principal) -> UUID | None:
        return principal.id if principal.kind is PrincipalKind.USER else None


def _duplicate_cin(existing_reference: str) -> DuplicateCINConflict:
    return DuplicateCINConflict(existing_reference)


def _required_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} is required.", field=field)
    clean = value.strip()
    if not clean:
        raise ValidationError(f"{field} is required.", field=field)
    if len(clean) > maximum:
        raise ValidationError(f"{field} must be at most {maximum} characters.", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in clean):
        raise ValidationError(f"{field} contains an invalid control character.", field=field)
    return clean


def _optional_text(value: object, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _clean_reference(value: object, field: str, *, maximum: int = 64) -> str:
    return _required_text(value, field, maximum=maximum)


def _currency(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("currency must be a three-letter code.", field="facility.currency")
    clean = value.strip().upper()
    if len(clean) != 3 or not clean.isalpha() or not clean.isascii():
        raise ValidationError("currency must be a three-letter code.", field="facility.currency")
    return clean


def _successor_reference(reference: str, version: int) -> str:
    suffix = f"-v{version + 1}"
    if len(reference) + len(suffix) <= 24:
        return f"{reference}{suffix}"
    return f"{reference[:15]}-{new_id().hex[:8]}"


__all__ = [
    "AuditWriter",
    "DuplicateCINConflict",
    "MASTER_DATA_READ_PERMISSION",
    "MASTER_DATA_WRITE_PERMISSION",
    "MasterDataService",
    "RevealedIdentity",
]
