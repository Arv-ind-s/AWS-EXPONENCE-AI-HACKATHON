"""Validate and load the application's versioned reference catalogs.

Reference data is application data, not test fixture data.  A catalog is
read from package-owned JSON files, validated in memory, and only then applied
inside one database transaction.  A malformed catalog or duplicate code
therefore leaves the database untouched.  Existing industry rows are kept
when a newer taxonomy omits them: the current schema has no retirement flag,
and retaining the row is the only portable way to keep historical industry
references resolvable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import ValidationError
from covenant_radar.db.models import (
    IndustryReference,
    Intervention,
    Permission,
    RatioDefinition,
    Role,
    RolePermission,
)

DEFAULT_DATA_DIR: Final[Path] = Path(__file__).resolve().parent / "data"
_TAXONOMY_VERSION_KEY = "taxonomy_version"
_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")
_MAX_CODE_LENGTH = 100
_MAX_DESCRIPTION_LENGTH = 500


class ReferenceDataError(ValidationError):
    """A catalog cannot be trusted and no seed changes were applied."""


@dataclass(frozen=True)
class SeedReport:
    """Outcome of one catalog load.

    Counts are grouped by table-like reference set.  ``retained`` counts
    industry rows omitted from a newer taxonomy but deliberately retained for
    historical resolution.  ``catalog_hash`` is the canonical hash of the
    validated input, independent of JSON whitespace and object-key order.
    """

    inserted: Mapping[str, int]
    updated: Mapping[str, int]
    retained: Mapping[str, int]
    catalog_hash: str

    @property
    def changed(self) -> bool:
        """Whether the database contents changed during this load."""
        return any(self.inserted.values()) or any(self.updated.values())

    @property
    def total_changes(self) -> int:
        """Return the number of inserted and updated reference rows."""
        return sum(self.inserted.values()) + sum(self.updated.values())


@dataclass(frozen=True)
class _CatalogFile:
    """A validated JSON document and its source path."""

    path: Path
    version: str
    rows: object
    key: str


class SeedLoader:
    """Load the checked-in reference catalogs into a SQLAlchemy session.

    ``load`` owns the transaction only when the supplied session is not
    already in one.  If a caller has an outer transaction, a savepoint is
    used so the caller retains control over the final commit or rollback.
    The session must be clean; silently flushing unrelated pending work while
    seeding would make the command's atomicity claim false.
    """

    _REQUIRED_FILES: Final[tuple[tuple[str, str], ...]] = (
        ("permissions.json", "permissions"),
        ("roles.json", "roles"),
        ("industries.json", "industries"),
        ("statement_lines.json", "statement_lines"),
        ("calendar.json", "calendar"),
    )

    def __init__(
        self,
        session: Session,
        *,
        data_dir: Path | str | None = None,
        clock: Clock | None = None,
        request_id: str | None = None,
    ) -> None:
        self._session = session
        self._data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        self._clock = clock or SystemClock()
        self._request_id = request_id or get_request_id() or new_request_id()

    def load(self) -> SeedReport:
        """Validate and atomically load all mandatory and present catalogs."""
        catalog = _read_catalog(self._data_dir)
        if self._session.new or self._session.dirty or self._session.deleted:
            raise ReferenceDataError(
                "Reference data seeding requires a clean database session; "
                "pending unrelated changes were found."
            )

        transaction = (
            self._session.begin_nested()
            if self._session.in_transaction()
            else self._session.begin()
        )
        try:
            with transaction:
                return self._apply(catalog)
        except IntegrityError as error:
            raise ReferenceDataError(
                "Reference data could not be loaded because the database rejected "
                "a reference row; no seed changes were committed."
            ) from error

    def load_all(self) -> SeedReport:
        """Compatibility spelling for callers that name the full catalog set."""
        return self.load()

    def _apply(self, catalog: Mapping[str, _CatalogFile]) -> SeedReport:
        now = _ensure_utc(self._clock.now())
        inserted: dict[str, int] = {}
        updated: dict[str, int] = {}
        retained: dict[str, int] = {}

        permission_rows = _rows(catalog["permissions.json"], "permissions")
        permissions_by_code = {
            row.code: row for row in self._session.scalars(select(Permission)).all()
        }
        for item in permission_rows:
            code = cast(str, item["code"])
            description = cast(str, item["description"])
            permission = permissions_by_code.get(code)
            if permission is None:
                permission = Permission(
                    code=code,
                    description=description,
                    **_standard_values(now, self._request_id),
                )
                self._session.add(permission)
                permissions_by_code[code] = permission
                _increment(inserted, "permissions")
            elif permission.description != description:
                permission.description = description
                permission.updated_at = now
                permission.updated_by_id = None
                permission.request_id = self._request_id
                _increment(updated, "permissions")
        self._session.flush()

        role_rows = _rows(catalog["roles.json"], "roles")
        roles_by_code = {row.code: row for row in self._session.scalars(select(Role)).all()}
        for item in role_rows:
            code = cast(str, item["code"])
            name = cast(str, item["name"])
            role = roles_by_code.get(code)
            if role is None:
                role = Role(
                    code=code,
                    name=name,
                    is_system=True,
                    **_standard_values(now, self._request_id),
                    version=1,
                )
                self._session.add(role)
                roles_by_code[code] = role
                _increment(inserted, "roles")
            else:
                role_changed = role.name != name or not role.is_system
                if role_changed:
                    role.name = name
                    role.is_system = True
                    role.version += 1
                    role.updated_at = now
                    role.updated_by_id = None
                    role.request_id = self._request_id
                    _increment(updated, "roles")
        self._session.flush()

        for item in role_rows:
            role = roles_by_code[cast(str, item["code"])]
            desired_codes = set(cast(list[str], item["permissions"]))
            desired_ids = {permissions_by_code[code].id for code in desired_codes}
            existing_links = self._session.scalars(
                select(RolePermission).where(RolePermission.role_id == role.id)
            ).all()
            existing_ids = {link.permission_id for link in existing_links}
            stale_links = [link for link in existing_links if link.permission_id not in desired_ids]
            for link in stale_links:
                self._session.delete(link)
                _increment(updated, "role_permissions")
            for permission_id in sorted(desired_ids - existing_ids, key=str):
                self._session.add(
                    RolePermission(
                        role_id=role.id,
                        permission_id=permission_id,
                        **_standard_values(now, self._request_id),
                    )
                )
                _increment(inserted, "role_permissions")

        industry_rows = _rows(catalog["industries.json"], "industries")
        industry_by_code = {
            row.code: row for row in self._session.scalars(select(IndustryReference)).all()
        }
        source_version = catalog["industries.json"].version
        ordered_industries = _topological_industries(industry_rows, set(industry_by_code))
        for item in ordered_industries:
            code = cast(str, item["code"])
            name = cast(str, item["name"])
            parent_code = cast(str | None, item.get("parent_code"))
            industry = industry_by_code.get(code)
            if industry is None:
                industry = IndustryReference(
                    code=code,
                    name=name,
                    parent_code=parent_code,
                    taxonomy_version=source_version,
                    **_standard_values(now, self._request_id),
                )
                self._session.add(industry)
                industry_by_code[code] = industry
                _increment(inserted, "industries")
                continue

            if _compare_versions(source_version, industry.taxonomy_version) < 0:
                continue
            industry_changed = (
                industry.name != name
                or industry.parent_code != parent_code
                or industry.taxonomy_version != source_version
            )
            if industry_changed:
                industry.name = name
                industry.parent_code = parent_code
                industry.taxonomy_version = source_version
                industry.updated_at = now
                industry.updated_by_id = None
                industry.request_id = self._request_id
                _increment(updated, "industries")

        existing_codes = set(industry_by_code)
        source_codes = {cast(str, item["code"]) for item in industry_rows}
        if _any_newer_industry_rows(industry_by_code.values(), source_version):
            retained["industries"] = len(existing_codes - source_codes)

        optional_ratio_file = catalog.get("ratio_definitions.json")
        if optional_ratio_file is not None:
            self._apply_ratio_definitions(optional_ratio_file, now, inserted, updated)

        optional_intervention_file = catalog.get("interventions.json")
        if optional_intervention_file is not None:
            self._apply_interventions(optional_intervention_file, now, inserted, updated)

        self._session.flush()
        return SeedReport(
            inserted=dict(inserted),
            updated=dict(updated),
            retained=dict(retained),
            catalog_hash=_catalog_hash(catalog),
        )

    def _apply_ratio_definitions(
        self,
        catalog_file: _CatalogFile,
        now: datetime,
        inserted: dict[str, int],
        updated: dict[str, int],
    ) -> None:
        rows = _rows(catalog_file, "ratio_definitions")
        definitions = {
            row.code: row for row in self._session.scalars(select(RatioDefinition)).all()
        }
        for item in rows:
            code = cast(str, item["code"])
            values = _ratio_values(item, catalog_file.version)
            definition = definitions.get(code)
            if definition is None:
                self._session.add(
                    RatioDefinition(**values, **_standard_values(now, self._request_id))
                )
                _increment(inserted, "ratio_definitions")
                continue
            if _compare_versions(catalog_file.version, definition.taxonomy_version) < 0:
                continue
            if any(getattr(definition, key) != value for key, value in values.items()):
                for key, value in values.items():
                    setattr(definition, key, value)
                definition.version += 1
                definition.updated_at = now
                definition.updated_by_id = None
                definition.request_id = self._request_id
                _increment(updated, "ratio_definitions")

    def _apply_interventions(
        self,
        catalog_file: _CatalogFile,
        now: datetime,
        inserted: dict[str, int],
        updated: dict[str, int],
    ) -> None:
        rows = _rows(catalog_file, "interventions")
        interventions = {row.code: row for row in self._session.scalars(select(Intervention)).all()}
        for item in rows:
            code = cast(str, item["code"])
            values = _intervention_values(item)
            intervention = interventions.get(code)
            if intervention is None:
                self._session.add(
                    Intervention(**values, **_standard_values(now, self._request_id), version=1)
                )
                _increment(inserted, "interventions")
                continue
            if any(getattr(intervention, key) != value for key, value in values.items()):
                for key, value in values.items():
                    setattr(intervention, key, value)
                intervention.version += 1
                intervention.updated_at = now
                intervention.updated_by_id = None
                intervention.request_id = self._request_id
                _increment(updated, "interventions")


ReferenceDataLoader = SeedLoader


def load_reference_data(
    session: Session,
    *,
    data_dir: Path | str | None = None,
    clock: Clock | None = None,
    request_id: str | None = None,
) -> SeedReport:
    """Convenience wrapper around :class:`SeedLoader`."""
    return SeedLoader(
        session,
        data_dir=data_dir,
        clock=clock,
        request_id=request_id,
    ).load()


def deterministic_catalog_hash(data_dir: Path | str | None = None) -> str:
    """Return the canonical hash of the validated reference catalogs."""
    return _catalog_hash(
        _read_catalog(Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR)
    )


def _read_catalog(data_dir: Path) -> dict[str, _CatalogFile]:
    if not data_dir.is_dir():
        raise ReferenceDataError(f"Reference data directory does not exist: {data_dir}")

    catalog: dict[str, _CatalogFile] = {}
    for filename, key in SeedLoader._REQUIRED_FILES:
        catalog[filename] = _read_catalog_file(data_dir / filename, key)

    optional_files = (
        ("ratio_definitions.json", "ratio_definitions"),
        ("interventions.json", "interventions"),
    )
    for filename, key in optional_files:
        path = data_dir / filename
        if path.exists():
            catalog[filename] = _read_catalog_file(path, key)

    _validate_cross_references(catalog)
    return catalog


def _read_catalog_file(path: Path, key: str) -> _CatalogFile:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReferenceDataError(f"Reference data file cannot be read: {path}") from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReferenceDataError(
            f"Invalid JSON in reference data file {path}, line {error.lineno}, "
            f"column {error.colno}: {error.msg}."
        ) from error
    if not isinstance(payload, dict):
        raise ReferenceDataError(f"Reference data file {path} must contain a JSON object.")
    if set(payload) != {_TAXONOMY_VERSION_KEY, key}:
        expected = f"'{_TAXONOMY_VERSION_KEY}' and '{key}'"
        raise ReferenceDataError(f"Reference data file {path} must contain only {expected}.")

    version = _text(payload[_TAXONOMY_VERSION_KEY], f"{path}:taxonomy_version")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ReferenceDataError(
            f"Reference data file {path} has invalid taxonomy_version {version!r}; "
            "expected dot-separated non-negative integers."
        )
    rows = payload[key]
    if key == "calendar":
        _validate_calendar(rows, path)
    else:
        if not isinstance(rows, list):
            raise ReferenceDataError(f"Reference data file {path} field '{key}' must be an array.")
        _validate_rows(rows, path, key)
    return _CatalogFile(path=path, version=version, rows=rows, key=key)


def _validate_rows(rows: list[object], path: Path, key: str) -> None:
    seen: set[str] = set()
    for position, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, dict):
            raise ReferenceDataError(f"{path} row {position} must be a JSON object.")
        row = cast(dict[str, object], raw_row)
        if "code" not in row:
            raise ReferenceDataError(f"{path} row {position} is missing 'code'.")
        code = _text(row["code"], f"{path} row {position}.code")
        if len(code) > _MAX_CODE_LENGTH or not _CODE_PATTERN.fullmatch(code):
            raise ReferenceDataError(f"{path} row {position} has invalid code {code!r}.")
        if code in seen:
            raise ReferenceDataError(f"Duplicate code {code!r} in {path}; nothing was loaded.")
        seen.add(code)
        if key == "permissions":
            _require_exact_keys(row, {"code", "description"}, path, position)
            description = _text(row["description"], f"{path} row {position}.description")
            if len(description) > _MAX_DESCRIPTION_LENGTH:
                raise ReferenceDataError(f"{path} row {position}.description is too long.")
        elif key == "roles":
            _require_exact_keys(row, {"code", "name", "permissions"}, path, position)
            _text(row["name"], f"{path} row {position}.name")
            permission_codes = row["permissions"]
            if not isinstance(permission_codes, list) or not all(
                isinstance(value, str) for value in permission_codes
            ):
                raise ReferenceDataError(
                    f"{path} row {position}.permissions must be an array of codes."
                )
            if len(set(permission_codes)) != len(permission_codes):
                duplicate = _first_duplicate(cast(list[str], permission_codes))
                raise ReferenceDataError(
                    f"Duplicate permission code {duplicate!r} in role {code!r}; nothing was loaded."
                )
        elif key == "industries":
            _require_exact_keys(row, {"code", "name", "parent_code"}, path, position)
            _text(row["name"], f"{path} row {position}.name")
            parent_code = row["parent_code"]
            if parent_code is not None:
                _text(parent_code, f"{path} row {position}.parent_code")
        elif key == "statement_lines":
            _require_exact_keys(
                row,
                {"code", "name", "statement", "sign_convention", "is_derived", "derivation"},
                path,
                position,
            )
            for field in ("name", "statement", "sign_convention"):
                _text(row[field], f"{path} row {position}.{field}")
            if not isinstance(row["is_derived"], bool):
                raise ReferenceDataError(f"{path} row {position}.is_derived must be boolean.")
            if row["is_derived"] and row["derivation"] is not None:
                _text(row["derivation"], f"{path} row {position}.derivation")
        elif key == "ratio_definitions":
            _validate_ratio_row(row, path, position)
        elif key == "interventions":
            _validate_intervention_row(row, path, position)


def _validate_cross_references(catalog: Mapping[str, _CatalogFile]) -> None:
    permissions = {
        cast(str, row["code"]) for row in _rows(catalog["permissions.json"], "permissions")
    }
    roles = _rows(catalog["roles.json"], "roles")
    for role in roles:
        unknown = set(cast(list[str], role["permissions"])) - permissions
        if unknown:
            unknown_codes = ", ".join(sorted(unknown))
            raise ReferenceDataError(
                f"Role {role['code']!r} references unknown permission code(s): {unknown_codes}."
            )

    industries = _rows(catalog["industries.json"], "industries")
    industry_codes = {cast(str, row["code"]) for row in industries}
    for row in industries:
        parent_code = row.get("parent_code")
        if parent_code is not None and parent_code not in industry_codes:
            raise ReferenceDataError(
                f"Industry {row['code']!r} references unknown parent code {parent_code!r}."
            )
    _topological_industries(industries, set())


def _validate_calendar(value: object, path: Path) -> None:
    if not isinstance(value, dict):
        raise ReferenceDataError(f"Reference data file {path} field 'calendar' must be an object.")
    calendar = cast(dict[str, object], value)
    expected = {
        "fiscal_year_start_month",
        "weekend_adjustment",
        "holiday_adjustment",
        "holidays",
    }
    _require_exact_keys(calendar, expected, path, None)
    month = calendar["fiscal_year_start_month"]
    if not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12:
        raise ReferenceDataError(
            f"{path}.calendar.fiscal_year_start_month must be between 1 and 12."
        )
    for field in ("weekend_adjustment", "holiday_adjustment"):
        _text(calendar[field], f"{path}.calendar.{field}")
    holidays = calendar["holidays"]
    if not isinstance(holidays, list):
        raise ReferenceDataError(f"{path}.calendar.holidays must be an array.")
    seen: set[str] = set()
    for position, holiday in enumerate(holidays, start=1):
        if not isinstance(holiday, dict):
            raise ReferenceDataError(f"{path}.calendar.holidays row {position} must be an object.")
        holiday_dict = cast(dict[str, object], holiday)
        _require_exact_keys(holiday_dict, {"date", "name"}, path, position)
        date_value = _text(holiday_dict["date"], f"{path}.calendar.holidays[{position}].date")
        name = _text(holiday_dict["name"], f"{path}.calendar.holidays[{position}].name")
        if date_value in seen:
            raise ReferenceDataError(f"Duplicate holiday date {date_value!r} in {path}.")
        seen.add(date_value)
        try:
            datetime.fromisoformat(date_value).date()
        except ValueError as error:
            raise ReferenceDataError(
                f"{path}.calendar.holidays[{position}].date is not an ISO date: {date_value!r}."
            ) from error
        if not name:
            raise ReferenceDataError(f"{path}.calendar.holidays[{position}].name cannot be empty.")


def _validate_ratio_row(row: dict[str, object], path: Path, position: int) -> None:
    expected = {
        "code",
        "name",
        "formula_text",
        "required_lines",
        "unit",
        "plausible_min",
        "plausible_max",
        "direction_hint",
    }
    _require_exact_keys(row, expected, path, position)
    for field in ("name", "formula_text", "unit"):
        _text(row[field], f"{path} row {position}.{field}")
    required_lines = row["required_lines"]
    if not isinstance(required_lines, list) or not all(
        isinstance(value, str) and value for value in required_lines
    ):
        raise ReferenceDataError(f"{path} row {position}.required_lines must be an array of names.")
    for field in ("plausible_min", "plausible_max"):
        _decimal(row[field], f"{path} row {position}.{field}", allow_none=True)
    direction = row["direction_hint"]
    if direction is not None and direction not in {"min", "max"}:
        raise ReferenceDataError(
            f"{path} row {position}.direction_hint must be 'min', 'max', or null."
        )


def _validate_intervention_row(row: dict[str, object], path: Path, position: int) -> None:
    expected = {
        "code",
        "role_tag",
        "text",
        "effect_model",
        "effect_parameters",
        "applicable_covenant_classes",
        "requires_approval",
        "is_active",
    }
    _require_exact_keys(row, expected, path, position)
    for field in ("text", "effect_model"):
        _text(row[field], f"{path} row {position}.{field}")
    for field in ("role_tag",):
        if row[field] is not None:
            _text(row[field], f"{path} row {position}.{field}")
    for field in ("effect_parameters", "applicable_covenant_classes"):
        if row[field] is not None and not isinstance(row[field], dict | list):
            raise ReferenceDataError(
                f"{path} row {position}.{field} must be an object, array, or null."
            )
    for field in ("requires_approval", "is_active"):
        if not isinstance(row[field], bool):
            raise ReferenceDataError(f"{path} row {position}.{field} must be boolean.")


def _rows(catalog_file: _CatalogFile, key: str) -> list[dict[str, object]]:
    if catalog_file.key != key or not isinstance(catalog_file.rows, list):
        raise ReferenceDataError(f"Reference catalog {catalog_file.path} does not contain '{key}'.")
    return [cast(dict[str, object], row) for row in catalog_file.rows]


def _topological_industries(
    rows: Sequence[dict[str, object]], existing_codes: set[str]
) -> list[dict[str, object]]:
    pending = list(rows)
    ordered: list[dict[str, object]] = []
    available = set(existing_codes)
    while pending:
        progress = False
        for row in pending[:]:
            parent_code = row.get("parent_code")
            if parent_code is None or parent_code in available:
                ordered.append(row)
                available.add(cast(str, row["code"]))
                pending.remove(row)
                progress = True
        if not progress:
            unresolved = ", ".join(sorted(cast(str, row["code"]) for row in pending))
            raise ReferenceDataError(
                f"Industry taxonomy contains a parent cycle or unresolved dependency: {unresolved}."
            )
    return ordered


def _ratio_values(row: dict[str, object], version: str) -> dict[str, object]:
    return {
        "code": cast(str, row["code"]),
        "name": cast(str, row["name"]),
        "formula_text": cast(str, row["formula_text"]),
        "required_lines": cast(list[str], row["required_lines"]),
        "unit": cast(str, row["unit"]),
        "plausible_min": _decimal(row["plausible_min"], "plausible_min", allow_none=True),
        "plausible_max": _decimal(row["plausible_max"], "plausible_max", allow_none=True),
        "direction_hint": cast(str | None, row["direction_hint"]),
        "taxonomy_version": version,
    }


def _intervention_values(row: dict[str, object]) -> dict[str, object]:
    return {
        "code": cast(str, row["code"]),
        "role_tag": cast(str | None, row["role_tag"]),
        "text": cast(str, row["text"]),
        "effect_model": cast(str, row["effect_model"]),
        "effect_parameters": cast(dict[str, object] | None, row["effect_parameters"]),
        "applicable_covenant_classes": cast(list[str] | None, row["applicable_covenant_classes"]),
        "requires_approval": cast(bool, row["requires_approval"]),
        "is_active": cast(bool, row["is_active"]),
        "retired_at": None,
    }


def _standard_values(now: datetime, request_id: str) -> dict[str, object]:
    return {
        "created_at": now,
        "updated_at": now,
        "created_by_id": None,
        "updated_by_id": None,
        "request_id": request_id,
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ReferenceDataError("The seed clock must return a timezone-aware datetime.")
    return value.astimezone(UTC)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceDataError(f"{field} must be a non-empty string.")
    return value


def _decimal(value: object, field: str, *, allow_none: bool) -> Decimal | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ReferenceDataError(f"{field} must be a decimal value or null.")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ReferenceDataError(f"{field} must be a decimal value or null.") from error


def _require_exact_keys(
    row: Mapping[str, object], expected: set[str], path: Path, position: int | None
) -> None:
    actual = set(row)
    if actual == expected:
        return
    location = str(path) if position is None else f"{path} row {position}"
    missing = ", ".join(sorted(expected - actual))
    extra = ", ".join(sorted(actual - expected))
    details = []
    if missing:
        details.append(f"missing {missing}")
    if extra:
        details.append(f"unknown {extra}")
    raise ReferenceDataError(f"{location} has invalid fields ({'; '.join(details)}).")


def _first_duplicate(values: Iterable[str]) -> str:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    raise AssertionError("_first_duplicate called without a duplicate")


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _compare_versions(left: str, right: str) -> int:
    left_parts = _version_key(left)
    right_parts = _version_key(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _any_newer_industry_rows(rows: Iterable[IndustryReference], source_version: str) -> bool:
    return any(_compare_versions(row.taxonomy_version, source_version) < 0 for row in rows)


def _catalog_hash(catalog: Mapping[str, _CatalogFile]) -> str:
    payload: dict[str, object] = {}
    for filename in sorted(catalog):
        catalog_file = catalog[filename]
        rows = catalog_file.rows
        if isinstance(rows, list):
            rows = sorted(
                rows,
                key=lambda row: str(row.get("code", "")) if isinstance(row, dict) else str(row),
            )
        payload[filename] = {
            _TAXONOMY_VERSION_KEY: catalog_file.version,
            catalog_file.key: rows,
        }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
