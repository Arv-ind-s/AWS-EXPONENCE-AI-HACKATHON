"""Deterministic synthetic Indian commercial-lending reference portfolio."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from random import Random
from types import MappingProxyType
from typing import Final
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.db.models import (
    Borrower,
    BorrowerContact,
    BorrowerGroup,
    Facility,
    FacilityConduct,
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    Portfolio,
    RelatedParty,
    SignalEvent,
    StatementLineValue,
)
from covenant_radar.db.seed.loader import DEFAULT_DATA_DIR
from covenant_radar.domain.ratios.compute import FacilityFacts
from covenant_radar.domain.statements.chart import Chart, default_chart
from evaluation.reference_portfolio.financials import (
    DEFAULT_FIRST_FISCAL_YEAR,
    FinancialPeriodRecord,
    generate_financial_periods,
)
from evaluation.reference_portfolio.names import (
    CONTACT_DESIGNATIONS,
    NameFactory,
    build_cin,
    build_contact_name,
    build_legal_name,
    build_pan,
    is_valid_cin,
    is_valid_pan,
)

DEFAULT_SEED: Final[int] = 20260830
DEFAULT_BORROWER_COUNT: Final[int] = 5_000
DEFAULT_FACILITY_COUNT: Final[int] = 12_000
DEFAULT_QUARTER_COUNT: Final[int] = 8
DEFAULT_MIN_LEGAL_NAME_LENGTH: Final[int] = 20
CRILC_MINIMUM_EXPOSURE_CRORE: Final[Decimal] = Decimal("5.00")
MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
#: Signal rows are inserted in batches of this size.  A full-size portfolio at
#: the default 365 days produces `borrowers * days * 6` events — millions of
#: rows — so they are streamed through SQLAlchemy Core `executemany` rather
#: than materialised in one list or one ORM flush.
_SIGNAL_CHUNK_ROWS: Final[int] = 20_000
_UUID_TIMESTAMP_BASE: Final[int] = 1_700_000_000_000
_FINANCIALS_MAPPING_NAME: Final[str] = "reference-portfolio-financials"
_FINANCIALS_SOURCE_REFERENCE: Final[str] = "evaluation/reference-portfolio-financials-v1"


class ReferencePortfolioError(ValueError):
    """Raised when a reference portfolio cannot be trusted or loaded."""


@dataclass(frozen=True, slots=True)
class ReferencePortfolioConfig:
    """Validated knobs for a deterministic reference build."""

    seed: int = DEFAULT_SEED
    borrower_count: int = DEFAULT_BORROWER_COUNT
    facility_count: int = DEFAULT_FACILITY_COUNT
    quarter_count: int = DEFAULT_QUARTER_COUNT
    first_fiscal_year: int = DEFAULT_FIRST_FISCAL_YEAR
    minimum_legal_name_length: int = DEFAULT_MIN_LEGAL_NAME_LENGTH

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer.")
        for field_name in ("borrower_count", "facility_count", "quarter_count"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer.")
        if self.borrower_count > 99_999:
            raise ValueError("borrower_count cannot exceed the available CIN sequence space.")
        if self.facility_count < self.borrower_count:
            raise ValueError("facility_count must be at least borrower_count.")
        if (
            not isinstance(self.minimum_legal_name_length, int)
            or isinstance(self.minimum_legal_name_length, bool)
            or self.minimum_legal_name_length < 1
        ):
            raise ValueError("minimum_legal_name_length must be a positive integer.")
        if (
            not isinstance(self.first_fiscal_year, int)
            or isinstance(self.first_fiscal_year, bool)
            or not 2000 <= self.first_fiscal_year <= 2099
        ):
            raise ValueError("first_fiscal_year must be between 2000 and 2099.")

    @property
    def borrowers(self) -> int:
        """Alias useful to callers that refer to the generated table size."""
        return self.borrower_count

    @property
    def facilities(self) -> int:
        """Alias useful to callers that refer to the generated table size."""
        return self.facility_count


DEFAULT_REFERENCE_CONFIG: Final[ReferencePortfolioConfig] = ReferencePortfolioConfig()


@dataclass(frozen=True, slots=True)
class BorrowerRecord:
    id: UUID
    reference: str
    legal_name: str
    cin: str
    pan: str
    industry_code: str
    group_id: UUID
    portfolio_code: str
    constitution: str
    incorporation_date: date
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class BorrowerGroupRecord:
    id: UUID
    name: str
    parent_id: UUID | None


@dataclass(frozen=True, slots=True)
class RelatedPartyRecord:
    id: UUID
    borrower_id: UUID
    party_type: str
    name: str
    identifier: str
    role: str


@dataclass(frozen=True, slots=True)
class ContactRecord:
    id: UUID
    borrower_id: UUID
    name: str
    email: str
    phone: str
    designation: str
    is_primary: bool


@dataclass(frozen=True, slots=True)
class FacilityRecord:
    id: UUID
    reference: str
    borrower_id: UUID
    facility_type: str
    sanctioned_limit: Decimal
    currency: str
    drawing_power: Decimal
    outstanding: Decimal
    security_type: str
    pricing_bps: int
    sanction_date: date
    maturity_date: date
    effective_from: date
    effective_to: date | None
    promoter_shareholding_floor_pct: Decimal


@dataclass(frozen=True, slots=True)
class ReferencePortfolio:
    """All generated tables plus their deterministic build configuration."""

    config: ReferencePortfolioConfig
    portfolio_code: str
    groups: tuple[BorrowerGroupRecord, ...]
    borrowers: tuple[BorrowerRecord, ...]
    related_parties: tuple[RelatedPartyRecord, ...]
    contacts: tuple[ContactRecord, ...]
    facilities: tuple[FacilityRecord, ...]
    financials: tuple[FinancialPeriodRecord, ...]

    @property
    def financial_periods(self) -> tuple[FinancialPeriodRecord, ...]:
        """Explicit table-name alias for consumers using the schema vocabulary."""
        return self.financials

    def content_hashes(self) -> Mapping[str, str]:
        """Hash each generated table using canonical, order-stable JSON."""
        tables: Mapping[str, Sequence[object]] = {
            "groups": self.groups,
            "borrowers": self.borrowers,
            "related_parties": self.related_parties,
            "contacts": self.contacts,
            "facilities": self.facilities,
            "financials": self.financials,
        }
        return MappingProxyType(
            {
                name: hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()
                for name, rows in tables.items()
            }
        )

    @property
    def table_hashes(self) -> Mapping[str, str]:
        """Alias for :meth:`content_hashes`."""
        return self.content_hashes()

    @property
    def content_hash(self) -> str:
        """Hash the table hashes into one concise build fingerprint."""
        hashes = self.content_hashes()
        payload = "\n".join(f"{name}:{hashes[name]}" for name in sorted(hashes))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_uuid(seed: int, table: str, sequence: int) -> UUID:
    """Create a deterministic UUIDv7-shaped identifier for generated rows."""
    if sequence < 1:
        raise ValueError("A generated row sequence must be positive.")
    digest = hashlib.sha256(f"{seed}:{table}:{sequence}".encode()).digest()
    table_digest = int.from_bytes(digest[:2], "big")
    timestamp_ms = _UUID_TIMESTAMP_BASE + table_digest * 100_000 + sequence
    random_bits = int.from_bytes(digest[2:10], "big") & ((1 << 62) - 1)
    sequence_bits = int.from_bytes(digest[10:12], "big") & 0xFFF
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= sequence_bits << 64
    value |= 0b10 << 62
    value |= random_bits
    return UUID(int=value)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _random_decimal(random_source: Random, lower: Decimal, upper: Decimal) -> Decimal:
    lower_units = int(lower * 100)
    upper_units = int(upper * 100)
    return Decimal(random_source.randint(lower_units, upper_units)) / Decimal(100)


def _partition_facilities(random_source: Random, borrowers: int, facilities: int) -> list[int]:
    counts = [1] * borrowers
    for _ in range(facilities - borrowers):
        counts[random_source.randrange(borrowers)] += 1
    return counts


def _facility_facts(record: FacilityRecord) -> FacilityFacts:
    return FacilityFacts(
        sanctioned_limit=record.sanctioned_limit,
        outstanding=record.outstanding,
        drawing_power=record.drawing_power,
        promoter_shareholding_floor_pct=record.promoter_shareholding_floor_pct,
    )


class ReferencePortfolioGenerator:
    """Build a complete portfolio from one isolated pseudo-random source."""

    def __init__(
        self,
        config: ReferencePortfolioConfig = DEFAULT_REFERENCE_CONFIG,
        *,
        name_factory: NameFactory = build_legal_name,
        chart: Chart | None = None,
    ) -> None:
        if not callable(name_factory):
            raise TypeError("name_factory must be callable.")
        self.config = config
        self.name_factory = name_factory
        self.chart = chart or default_chart()

    def generate(self) -> ReferencePortfolio:
        random_source = Random(self.config.seed)
        portfolio_code = "REF-PORTFOLIO"
        groups = self._generate_groups(random_source)
        borrowers = self._generate_borrowers(random_source, groups, portfolio_code)
        related_parties, contacts = self._generate_people(random_source, borrowers)
        facilities = self._generate_facilities(random_source, borrowers)
        facilities_by_borrower: dict[UUID, list[FacilityRecord]] = defaultdict(list)
        for facility in facilities:
            facilities_by_borrower[facility.borrower_id].append(facility)

        financials: list[FinancialPeriodRecord] = []
        for borrower_index, borrower in enumerate(borrowers, start=1):
            periods = tuple(
                _stable_uuid(
                    self.config.seed,
                    "financial_period",
                    (borrower_index - 1) * self.config.quarter_count + offset,
                )
                for offset in range(1, self.config.quarter_count + 1)
            )
            first_facility = facilities_by_borrower[borrower.id][0]
            financials.extend(
                generate_financial_periods(
                    random_source,
                    borrower_id=borrower.id,
                    period_ids=periods,
                    first_fiscal_year=self.config.first_fiscal_year,
                    quarter_count=self.config.quarter_count,
                    facility=_facility_facts(first_facility),
                    chart=self.chart,
                )
            )

        result = ReferencePortfolio(
            config=self.config,
            portfolio_code=portfolio_code,
            groups=tuple(groups),
            borrowers=tuple(borrowers),
            related_parties=tuple(related_parties),
            contacts=tuple(contacts),
            facilities=tuple(facilities),
            financials=tuple(financials),
        )
        self._validate(result)
        return result

    def _generate_groups(self, random_source: Random) -> list[BorrowerGroupRecord]:
        group_count = max(1, (self.config.borrower_count + 9) // 10)
        # Consume a stable draw per group so changing the group-name corpus
        # cannot alter the borrower sequence that follows.
        for _ in range(group_count):
            random_source.random()
        return [
            BorrowerGroupRecord(
                id=_stable_uuid(self.config.seed, "group", index),
                name=f"Indian Commercial Group {index:04d} Private Limited",
                parent_id=None,
            )
            for index in range(1, group_count + 1)
        ]

    def _generate_borrowers(
        self,
        random_source: Random,
        groups: Sequence[BorrowerGroupRecord],
        portfolio_code: str,
    ) -> list[BorrowerRecord]:
        industry_codes = _industry_codes()
        borrowers: list[BorrowerRecord] = []
        for index in range(1, self.config.borrower_count + 1):
            legal_name = self.name_factory(random_source, index)
            if (
                not isinstance(legal_name, str)
                or len(legal_name.strip()) < self.config.minimum_legal_name_length
            ):
                raise ReferencePortfolioError(
                    f"Borrower {index} has a legal name shorter than the required "
                    f"{self.config.minimum_legal_name_length} characters."
                )
            cin = build_cin(index, random_source)
            pan = build_pan(index, random_source)
            if not is_valid_cin(cin) or not is_valid_pan(pan):
                raise ReferencePortfolioError(
                    f"Generated identity for borrower {index} is invalid."
                )
            incorporation_year = 1990 + (index % 30)
            borrowers.append(
                BorrowerRecord(
                    id=_stable_uuid(self.config.seed, "borrower", index),
                    reference=f"B-{index:06d}",
                    legal_name=legal_name.strip(),
                    cin=cin,
                    pan=pan,
                    industry_code=industry_codes[(index - 1) % len(industry_codes)],
                    group_id=groups[(index - 1) % len(groups)].id,
                    portfolio_code=portfolio_code,
                    constitution="private_limited",
                    incorporation_date=date(incorporation_year, 4, 1),
                )
            )
        return borrowers

    def _generate_people(
        self, random_source: Random, borrowers: Sequence[BorrowerRecord]
    ) -> tuple[list[RelatedPartyRecord], list[ContactRecord]]:
        related: list[RelatedPartyRecord] = []
        contacts: list[ContactRecord] = []
        for index, borrower in enumerate(borrowers, start=1):
            related.append(
                RelatedPartyRecord(
                    id=_stable_uuid(self.config.seed, "related_party", index),
                    borrower_id=borrower.id,
                    party_type="promoter",
                    name=build_contact_name(random_source),
                    identifier=f"PROM-{index:06d}",
                    role="Promoter and authorised signatory",
                )
            )
            name = build_contact_name(random_source)
            email_local = f"finance{index:06d}"
            contacts.append(
                ContactRecord(
                    id=_stable_uuid(self.config.seed, "contact", index),
                    borrower_id=borrower.id,
                    name=name,
                    email=f"{email_local}@reference.invalid",
                    phone=f"+91-90000-{index:05d}",
                    designation=CONTACT_DESIGNATIONS[(index - 1) % len(CONTACT_DESIGNATIONS)],
                    is_primary=True,
                )
            )
        return related, contacts

    def _generate_facilities(
        self, random_source: Random, borrowers: Sequence[BorrowerRecord]
    ) -> list[FacilityRecord]:
        counts = _partition_facilities(random_source, len(borrowers), self.config.facility_count)
        facilities: list[FacilityRecord] = []
        facility_sequence = 0
        for borrower_index, (borrower, count) in enumerate(
            zip(borrowers, counts, strict=True), start=1
        ):
            for facility_number in range(1, count + 1):
                facility_sequence += 1
                limit = _random_decimal(random_source, CRILC_MINIMUM_EXPOSURE_CRORE, Decimal("500"))
                drawing_pct = _random_decimal(random_source, Decimal("0.85"), Decimal("1.00"))
                outstanding_pct = _random_decimal(random_source, Decimal("0.35"), Decimal("0.90"))
                drawing_power = _money(limit * drawing_pct)
                outstanding = _money(drawing_power * outstanding_pct)
                sanction_date = date(2018 + (borrower_index % 7), 4, 1)
                maturity_date = date(sanction_date.year + 5 + facility_number % 3, 3, 31)
                facilities.append(
                    FacilityRecord(
                        id=_stable_uuid(self.config.seed, "facility", facility_sequence),
                        reference=f"F-{borrower_index:06d}-{facility_number:02d}",
                        borrower_id=borrower.id,
                        facility_type=("cash_credit" if facility_number % 2 else "term_loan"),
                        sanctioned_limit=limit,
                        currency="INR",
                        drawing_power=drawing_power,
                        outstanding=outstanding,
                        security_type="first_charge_on_current_assets",
                        pricing_bps=850 + (facility_sequence % 500),
                        sanction_date=sanction_date,
                        maturity_date=maturity_date,
                        effective_from=sanction_date,
                        effective_to=None,
                        promoter_shareholding_floor_pct=Decimal("40.00"),
                    )
                )
        return facilities

    def _validate(self, portfolio: ReferencePortfolio) -> None:
        expected_financials = self.config.borrower_count * self.config.quarter_count
        if len(portfolio.groups) != max(1, (self.config.borrower_count + 9) // 10):
            raise ReferencePortfolioError("Generated group count does not match the configuration.")
        if len(portfolio.borrowers) != self.config.borrower_count:
            raise ReferencePortfolioError(
                "Generated borrower count does not match the configuration."
            )
        if len(portfolio.facilities) != self.config.facility_count:
            raise ReferencePortfolioError(
                "Generated facility count does not match the configuration."
            )
        if len(portfolio.financials) != expected_financials:
            raise ReferencePortfolioError("Generated financial-period count is incomplete.")
        _require_unique(portfolio.borrowers, "reference")
        _require_unique(portfolio.facilities, "reference")
        _require_unique(portfolio.facilities, "id")
        _require_unique(portfolio.borrowers, "id")
        if any(
            facility.sanctioned_limit < CRILC_MINIMUM_EXPOSURE_CRORE
            for facility in portfolio.facilities
        ):
            raise ReferencePortfolioError("A generated facility is below the CRILC exposure band.")
        if any(
            len(borrower.legal_name) < self.config.minimum_legal_name_length
            for borrower in portfolio.borrowers
        ):
            raise ReferencePortfolioError(
                "A generated borrower name is below the realistic length floor."
            )


def _require_unique(rows: Sequence[object], attribute: str) -> None:
    values = [getattr(row, attribute) for row in rows]
    if len(values) != len(set(values)):
        raise ReferencePortfolioError(f"Generated {attribute} values are not unique.")


def _industry_codes() -> tuple[str, ...]:
    path = DEFAULT_DATA_DIR / "industries.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload["industries"]
        codes = tuple(row["code"] for row in rows if row.get("parent_code") is not None)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReferencePortfolioError(f"Industry catalog cannot be read: {path}.") from error
    if not codes:
        raise ReferencePortfolioError("Industry catalog contains no leaf industry codes.")
    return codes


def generate_reference_portfolio(
    config: ReferencePortfolioConfig | None = None,
    *,
    seed: int | None = None,
    borrower_count: int | None = None,
    facility_count: int | None = None,
    quarter_count: int | None = None,
    name_factory: NameFactory = build_legal_name,
) -> ReferencePortfolio:
    """Generate one deterministic reference portfolio.

    Keyword overrides are provided for small offline development builds.  A
    caller may either pass a complete config or use the defaults plus these
    overrides, but not both for the same field.
    """
    if config is not None and any(
        value is not None for value in (seed, borrower_count, facility_count, quarter_count)
    ):
        raise TypeError("Pass either config or size overrides, not both.")
    resolved = config or ReferencePortfolioConfig(
        seed=DEFAULT_SEED if seed is None else seed,
        borrower_count=DEFAULT_BORROWER_COUNT if borrower_count is None else borrower_count,
        facility_count=DEFAULT_FACILITY_COUNT if facility_count is None else facility_count,
        quarter_count=DEFAULT_QUARTER_COUNT if quarter_count is None else quarter_count,
    )
    return ReferencePortfolioGenerator(resolved, name_factory=name_factory).generate()


def _assert_portfolio_area_empty(session: Session) -> None:
    existing = {
        "portfolio": session.scalar(select(Portfolio.id).limit(1)),
        "borrower": session.scalar(select(Borrower.id).limit(1)),
        "facility": session.scalar(select(Facility.id).limit(1)),
    }
    present = ", ".join(name for name, value in existing.items() if value is not None)
    if present:
        raise ReferencePortfolioError(
            f"Reference portfolio generation refused: database is non-empty ({present}); "
            "use an explicit reset or a separate development database."
        )


def _standard_values(now: datetime, request_id: str) -> dict[str, object]:
    return {
        "created_at": now,
        "updated_at": now,
        "created_by_id": None,
        "updated_by_id": None,
        "request_id": request_id,
        "version": 1,
    }


def _unversioned_values(now: datetime, request_id: str) -> dict[str, object]:
    """`_standard_values` without `version`, for tables with no `VersionedColumns`."""
    return {
        "created_at": now,
        "updated_at": now,
        "created_by_id": None,
        "updated_by_id": None,
        "request_id": request_id,
    }


def load_reference_portfolio(
    session: Session,
    portfolio: ReferencePortfolio,
    *,
    clock: Clock | None = None,
    request_id: str | None = None,
    allow_non_empty: bool = False,
    signal_days: int | None = None,
) -> None:
    """Atomically add the generated portfolio, financials included, to an
    empty database area."""
    if not isinstance(session, Session):
        raise TypeError("load_reference_portfolio requires a SQLAlchemy Session.")
    if not isinstance(portfolio, ReferencePortfolio):
        raise TypeError("portfolio must be a ReferencePortfolio.")
    if not allow_non_empty:
        _assert_portfolio_area_empty(session)
    now = (clock or SystemClock()).now()
    if now.tzinfo is None:
        raise ValueError("Reference portfolio load requires an aware clock timestamp.")
    resolved_request_id = request_id or new_request_id()
    if not 1 <= len(resolved_request_id) <= 40:
        raise ValueError("request_id must be between 1 and 40 characters.")

    portfolio_row = Portfolio(
        id=_stable_uuid(portfolio.config.seed, "portfolio", 1),
        code=portfolio.portfolio_code,
        name="Synthetic Reference Portfolio",
        parent_id=None,
        branch_code="REF",
        path=f"{_stable_uuid(portfolio.config.seed, 'portfolio', 1).hex}/",
        **_standard_values(now, resolved_request_id),
    )
    session.add(portfolio_row)
    session.flush()

    session.add_all(
        [
            BorrowerGroup(
                id=record.id,
                name=record.name,
                parent_id=record.parent_id,
                **_standard_values(now, resolved_request_id),
            )
            for record in portfolio.groups
        ]
    )
    session.flush()

    session.add_all(
        [
            Borrower(
                id=record.id,
                reference=record.reference,
                legal_name=record.legal_name,
                cin_enc=record.cin,
                pan_enc=record.pan,
                cin_fingerprint=hashlib.sha256(record.cin.encode("ascii")).hexdigest(),
                industry_code=record.industry_code,
                group_id=record.group_id,
                portfolio_id=portfolio_row.id,
                constitution=record.constitution,
                incorporation_date=record.incorporation_date,
                is_active=record.is_active,
                **_standard_values(now, resolved_request_id),
            )
            for record in portfolio.borrowers
        ]
    )
    session.flush()

    # These values are synthetic and never leave the offline evaluation
    # build.  The application write path's configured field encryptor remains
    # the authority for customer data.
    session.add_all(
        [
            RelatedParty(
                id=record.id,
                borrower_id=record.borrower_id,
                party_type=record.party_type,
                name_enc=record.name,
                identifier_enc=record.identifier,
                role=record.role,
                effective_from=None,
                effective_to=None,
                **_standard_values(now, resolved_request_id),
            )
            for record in portfolio.related_parties
        ]
        + [
            BorrowerContact(
                id=record.id,
                borrower_id=record.borrower_id,
                name_enc=record.name,
                email_enc=record.email,
                phone_enc=record.phone,
                designation=record.designation,
                is_primary=record.is_primary,
                **_standard_values(now, resolved_request_id),
            )
            for record in portfolio.contacts
        ]
    )
    session.flush()

    session.add_all(
        [
            Facility(
                id=record.id,
                reference=record.reference,
                borrower_id=record.borrower_id,
                facility_type=record.facility_type,
                sanctioned_limit=record.sanctioned_limit,
                currency=record.currency,
                drawing_power=record.drawing_power,
                outstanding=record.outstanding,
                security_type=record.security_type,
                pricing_bps=record.pricing_bps,
                sanction_date=record.sanction_date,
                maturity_date=record.maturity_date,
                effective_from=record.effective_from,
                effective_to=record.effective_to,
                superseded_by_id=None,
                **_standard_values(now, resolved_request_id),
            )
            for record in portfolio.facilities
        ]
    )
    session.flush()

    _load_financials(session, portfolio, now=now, request_id=resolved_request_id)
    _load_signals(
        session,
        portfolio,
        signal_days=signal_days,
        now=now,
        request_id=resolved_request_id,
    )


def _load_financials(
    session: Session,
    portfolio: ReferencePortfolio,
    *,
    now: datetime,
    request_id: str,
) -> None:
    """Persist the generated financial periods and their statement lines.

    Written with SQLAlchemy Core bulk inserts rather than one ORM object per
    row: a full-size portfolio (`DEFAULT_QUARTER_COUNT` * `DEFAULT_BORROWER_COUNT`
    periods, ~28 lines each) is on the order of a million statement-line
    rows, and none of them need the ORM identity map after this call returns.
    """
    if not portfolio.financials:
        return

    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == _FINANCIALS_MAPPING_NAME, ImportMapping.version == 1
        )
    )
    if mapping is None:
        mapping = ImportMapping(
            id=_stable_uuid(portfolio.config.seed, "financials_mapping", 1),
            name=_FINANCIALS_MAPPING_NAME,
            source_type="json",
            version=1,
            spec={"mapping_version": 1, "purpose": "Synthetic reference portfolio financials"},
            is_active=True,
            **_unversioned_values(now, request_id),
        )
        session.add(mapping)
        session.flush()

    batch_content_hash = hashlib.sha256(
        f"{_FINANCIALS_SOURCE_REFERENCE}:{portfolio.content_hash}".encode()
    ).hexdigest()
    batch = ImportBatch(
        id=_stable_uuid(portfolio.config.seed, "financials_batch", 1),
        source_type="json",
        source_reference=_FINANCIALS_SOURCE_REFERENCE,
        mapping_id=mapping.id,
        content_hash=batch_content_hash,
        started_at=now,
        finished_at=now,
        row_count=len(portfolio.financials),
        accepted_count=len(portfolio.financials),
        quarantined_count=0,
        state="completed",
        report={"seed": portfolio.config.seed, "periods": len(portfolio.financials)},
        **_unversioned_values(now, request_id),
    )
    session.add(batch)
    session.flush()

    provenance = FieldProvenance(
        id=_stable_uuid(portfolio.config.seed, "financials_provenance", 1),
        source_type="json",
        source_reference=_FINANCIALS_SOURCE_REFERENCE,
        row_reference=None,
        mapping_version=1,
        ingested_at=now,
        batch_id=batch.id,
        transform_note="Synthetic reference-portfolio statement, generated deterministically.",
        **_unversioned_values(now, request_id),
    )
    session.add(provenance)
    session.flush()

    period_values = _standard_values(now, request_id)
    session.execute(
        insert(FinancialPeriod),
        [
            {
                "id": record.id,
                "borrower_id": record.borrower_id,
                "fy_label": record.fy_label,
                "period_type": record.period_type,
                "period_start": record.period_start,
                "period_end": record.period_end,
                "is_complete": record.is_complete,
                "is_audited": record.is_audited,
                "superseded_by_id": None,
                "source_batch_id": batch.id,
                **period_values,
            }
            for record in portfolio.financials
        ],
    )

    line_values = _unversioned_values(now, request_id)
    line_rows: list[dict[str, object]] = []
    sequence = 0
    for record in portfolio.financials:
        for line_code, value in record.lines.items():
            sequence += 1
            line_rows.append(
                {
                    "id": _stable_uuid(portfolio.config.seed, "statement_line_value", sequence),
                    "period_id": record.id,
                    "line_code": line_code,
                    "value": value,
                    "unit": "crore",
                    "currency": "INR",
                    "provenance_id": provenance.id,
                    **line_values,
                }
            )
    session.execute(insert(StatementLineValue), line_rows)


def _load_signals(
    session: Session,
    portfolio: ReferencePortfolio,
    *,
    signal_days: int | None,
    now: datetime,
    request_id: str,
) -> tuple[int, int]:
    """Persist the generated signal stream and the conduct series beside it.

    Without this the evidence ledger has no input at all: persistence and
    materiality scoring (`spec §3.2` stage 3) run over `signal_event`, and
    SMA banding (`R-08`) runs over `facility_conduct`.  A portfolio loaded
    without them produces forecasts whose signal-pressure term is always
    zero and an evidence screen that is empty by construction rather than
    because nothing happened.

    Conduct is derived from the same stream rather than generated separately
    so the two cannot disagree: the day a borrower's `payment` signal says
    nine days past due is the day its conduct row says the same.

    Returns `(signal_rows, conduct_rows)`.
    """

    # Imported here rather than at module scope: both `cohorts` and `signals`
    # import this module for their portfolio record types, so a top-level
    # import in either direction is circular.
    from evaluation.reference_portfolio.cohorts import assign_cohorts
    from evaluation.reference_portfolio.signals import (
        DEFAULT_SIGNAL_DAYS,
        _canonical_value,
        generate_signal_events,
    )

    resolved_days = DEFAULT_SIGNAL_DAYS if signal_days is None else signal_days
    # Deliberately assignments plus the stream, not `generate_reference_cohorts`:
    # that also derives and *verifies* outcome labels, which is the evaluation
    # harness's contract, not the seed's.  Label verification requires enough
    # runway for the deteriorating cohort to cross its threshold, so routing
    # the seed through it would make `--signal-days` fail below roughly a
    # quarter for a reason that has nothing to do with loading rows.
    assignments = assign_cohorts(portfolio)
    stream = generate_signal_events(
        portfolio,
        {assignment.borrower_id: assignment.cohort for assignment in assignments},
        signal_days=resolved_days,
    )

    signal_values = _unversioned_values(now, request_id)
    facility_currency = {facility.id: facility.currency for facility in portfolio.facilities}
    facility_limit = {
        facility.id: facility.sanctioned_limit for facility in portfolio.facilities
    }

    signal_rows: list[dict[str, object]] = []
    # Keyed by (facility_id, date) because one conduct row carries the whole
    # day: the payment family supplies days-past-due and the utilisation
    # family the drawn percentage, and both land on the same row.
    conduct: dict[tuple[UUID, date], dict[str, object]] = {}
    signal_count = 0

    def flush_signals() -> None:
        nonlocal signal_rows
        if signal_rows:
            session.execute(insert(SignalEvent), signal_rows)
            signal_rows = []

    for record in stream:
        signal_count += 1
        signal_rows.append(
            {
                "id": record.id,
                "borrower_id": record.borrower_id,
                "facility_id": record.facility_id,
                "event_date": record.event_date,
                "family": record.family,
                "event_type": record.event_type,
                "magnitude": record.magnitude,
                "unit": record.unit,
                # Canonicalised with the signals module's own encoder so the
                # stored payload is byte-identical to the form `content_hash`
                # was computed over, and JSON-serialisable: the raw payload
                # holds `Decimal` values the JSON column cannot bind.
                "payload": _canonical_value(record.payload),
                # The stream's `source_id` names the generator, not a row in
                # `feed_source`; recording a dangling reference would be worse
                # than recording none, and the payload already carries it.
                "source_id": None,
                "content_hash": record.content_hash,
                "is_late": record.is_late,
                "ingested_at": now,
                **signal_values,
            }
        )
        if len(signal_rows) >= _SIGNAL_CHUNK_ROWS:
            flush_signals()

        if record.family not in {"payment", "utilisation"}:
            continue
        key = (record.facility_id, record.event_date)
        row = conduct.get(key)
        if row is None:
            row = {
                "id": _stable_uuid(
                    portfolio.config.seed,
                    "facility_conduct",
                    len(conduct) + 1,
                ),
                "facility_id": record.facility_id,
                "as_of_date": record.event_date,
                "outstanding": None,
                "utilisation_pct": None,
                "days_past_due": None,
                "overdue_amount": None,
                "excess_amount": None,
                "source_id": None,
                **signal_values,
            }
            conduct[key] = row
        if record.family == "payment":
            row["days_past_due"] = int(record.magnitude)
        else:
            limit = facility_limit.get(record.facility_id)
            row["utilisation_pct"] = record.magnitude
            if limit is not None:
                drawn = (limit * record.magnitude / Decimal(100)).quantize(Decimal("0.01"))
                row["outstanding"] = drawn
                row["excess_amount"] = drawn - limit if drawn > limit else Decimal("0.00")

    flush_signals()

    conduct_rows = list(conduct.values())
    for start in range(0, len(conduct_rows), _SIGNAL_CHUNK_ROWS):
        session.execute(insert(FacilityConduct), conduct_rows[start : start + _SIGNAL_CHUNK_ROWS])

    # Currency is carried on the facility, so a conduct row never restates it;
    # this lookup exists only to fail loudly if the stream ever references a
    # facility the portfolio does not contain.
    unknown = {
        facility_id
        for facility_id, _ in conduct
        if facility_id not in facility_currency
    }
    if unknown:
        raise ReferencePortfolioError(
            f"Signal stream referenced {len(unknown)} facilities outside the portfolio."
        )

    return signal_count, len(conduct_rows)


def clear_reference_portfolio(session: Session) -> None:
    """Remove only the reserved reference portfolio from a development DB.

    The operation is intentionally narrow: if the reserved root does not
    exist, no rows are removed and the normal non-empty guard remains in
    force for any customer data already present.
    """
    if not isinstance(session, Session):
        raise TypeError("clear_reference_portfolio requires a SQLAlchemy Session.")
    root = session.scalar(select(Portfolio).where(Portfolio.code == "REF-PORTFOLIO").limit(1))
    if root is None:
        return
    borrower_ids = tuple(
        session.scalars(select(Borrower.id).where(Borrower.portfolio_id == root.id)).all()
    )
    facility_ids = tuple(
        session.scalars(select(Facility.id).where(Facility.borrower_id.in_(borrower_ids))).all()
        if borrower_ids
        else ()
    )
    if facility_ids:
        session.execute(
            delete(FacilityConduct).where(FacilityConduct.facility_id.in_(facility_ids))
        )
        # Signals are keyed by borrower rather than facility, but every
        # generated event names a facility, so clearing by facility removes
        # the same set while keeping the delete inside the reserved portfolio.
        session.execute(delete(SignalEvent).where(SignalEvent.facility_id.in_(facility_ids)))
        session.execute(delete(Facility).where(Facility.id.in_(facility_ids)))
    if borrower_ids:
        period_rows = session.execute(
            select(FinancialPeriod.id, FinancialPeriod.source_batch_id).where(
                FinancialPeriod.borrower_id.in_(borrower_ids)
            )
        ).all()
        period_ids = tuple(row[0] for row in period_rows)
        batch_ids = tuple({row[1] for row in period_rows if row[1] is not None})
        if period_ids:
            session.execute(
                delete(StatementLineValue).where(StatementLineValue.period_id.in_(period_ids))
            )
            session.execute(delete(FinancialPeriod).where(FinancialPeriod.id.in_(period_ids)))
        if batch_ids:
            session.execute(
                delete(FieldProvenance).where(FieldProvenance.batch_id.in_(batch_ids))
            )
            mapping_ids = tuple(
                set(
                    session.scalars(
                        select(ImportBatch.mapping_id).where(ImportBatch.id.in_(batch_ids))
                    ).all()
                )
            )
            session.execute(delete(ImportBatch).where(ImportBatch.id.in_(batch_ids)))
            if mapping_ids:
                still_used = session.scalar(
                    select(ImportBatch.id).where(ImportBatch.mapping_id.in_(mapping_ids)).limit(1)
                )
                if still_used is None:
                    session.execute(
                        delete(ImportMapping).where(ImportMapping.id.in_(mapping_ids))
                    )
        session.execute(delete(RelatedParty).where(RelatedParty.borrower_id.in_(borrower_ids)))
        session.execute(
            delete(BorrowerContact).where(BorrowerContact.borrower_id.in_(borrower_ids))
        )
        group_ids = tuple(
            session.scalars(
                select(BorrowerGroup.id).where(
                    BorrowerGroup.id.in_(
                        select(Borrower.group_id).where(
                            Borrower.id.in_(borrower_ids), Borrower.group_id.is_not(None)
                        )
                    )
                )
            ).all()
        )
        session.execute(delete(Borrower).where(Borrower.id.in_(borrower_ids)))
        if group_ids:
            session.execute(delete(BorrowerGroup).where(BorrowerGroup.id.in_(group_ids)))
    session.delete(root)
    session.flush()


def deterministic_reference_hashes(
    config: ReferencePortfolioConfig = DEFAULT_REFERENCE_CONFIG,
) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Build twice and return both table-hash sets for a determinism check."""
    first = generate_reference_portfolio(config)
    second = generate_reference_portfolio(config)
    first_hashes = first.content_hashes()
    second_hashes = second.content_hashes()
    if first_hashes != second_hashes:
        raise ReferencePortfolioError(
            f"Reference portfolio is not deterministic: {first_hashes} != {second_hashes}."
        )
    return first_hashes, second_hashes


__all__ = [
    "BorrowerGroupRecord",
    "BorrowerRecord",
    "ContactRecord",
    "CRILC_MINIMUM_EXPOSURE_CRORE",
    "DEFAULT_REFERENCE_CONFIG",
    "FacilityRecord",
    "ReferencePortfolio",
    "ReferencePortfolioConfig",
    "ReferencePortfolioError",
    "ReferencePortfolioGenerator",
    "RelatedPartyRecord",
    "clear_reference_portfolio",
    "deterministic_reference_hashes",
    "generate_reference_portfolio",
    "load_reference_portfolio",
]
