"""Versioned CRILC layout definitions, loaded from this package's JSON files.

Layouts are data, not code: `spec §R-31.a` requires the export to validate
against "the published layout," and a layout that lived only as Python
literals would make a version bump indistinguishable from an ordinary code
change. Each version is its own JSON file, named
``<report_type>.v<version>.json``, and a previous version is never edited
or removed once shipped — `load_crilc_layout` can always load any version
whose file remains in this directory, so a report regenerated against a
superseded layout still reproduces its historical shape (`spec §R-31.c`).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Final

from covenant_radar.reporting.crilc import CrilcLayout, CrilcLayoutField, CrilcReportType

_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parent
_FILENAME_TEMPLATE: Final[str] = "{report_type}.v{version}.json"


class CrilcLayoutNotFound(LookupError):
    """No layout file exists for the requested report type and version."""


def available_layout_versions(
    report_type: CrilcReportType | str,
    *,
    layouts_dir: Path | str | None = None,
) -> tuple[int, ...]:
    """Return every version shipped for `report_type`, ascending.

    Both the currently-default version and every superseded one are
    returned — nothing here is a "latest only" view — because
    `spec §R-31.c`'s "a layout version change retains both" is a claim
    about what remains loadable, not just about what is shipped today.
    """
    resolved_type = _coerce_report_type(report_type)
    directory = Path(layouts_dir) if layouts_dir is not None else _PACKAGE_DIR
    prefix = f"{resolved_type.value}.v"
    versions: list[int] = []
    for path in directory.glob(f"{prefix}*.json"):
        suffix = path.name[len(prefix) : -len(".json")]
        if suffix.isdigit():
            versions.append(int(suffix))
    return tuple(sorted(versions))


def load_crilc_layout(
    report_type: CrilcReportType | str,
    version: int | None = None,
    *,
    layouts_dir: Path | str | None = None,
) -> CrilcLayout:
    """Load one versioned layout, defaulting to the latest shipped version."""
    resolved_type = _coerce_report_type(report_type)
    directory = Path(layouts_dir) if layouts_dir is not None else _PACKAGE_DIR
    resolved_version = version
    if resolved_version is None:
        versions = available_layout_versions(resolved_type, layouts_dir=directory)
        if not versions:
            raise CrilcLayoutNotFound(
                f"No CRILC layout is published for {resolved_type.value!r}."
            )
        resolved_version = versions[-1]
    elif isinstance(resolved_version, bool) or not isinstance(resolved_version, int):
        raise TypeError("load_crilc_layout version must be an integer or None.")

    path = directory / _FILENAME_TEMPLATE.format(
        report_type=resolved_type.value, version=resolved_version
    )
    if not path.is_file():
        raise CrilcLayoutNotFound(
            f"No CRILC layout file for {resolved_type.value!r} version {resolved_version}."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _layout_from_mapping(raw, expected_type=resolved_type, expected_version=resolved_version)


def _coerce_report_type(value: CrilcReportType | str) -> CrilcReportType:
    if isinstance(value, CrilcReportType):
        return value
    try:
        return CrilcReportType(value)
    except ValueError as error:
        raise ValueError(f"Unknown CRILC report type: {value!r}.") from error


def _layout_from_mapping(
    raw: object, *, expected_type: CrilcReportType, expected_version: int
) -> CrilcLayout:
    if not isinstance(raw, dict):
        raise ValueError("A CRILC layout file must contain a JSON object.")
    mapping: dict[str, object] = raw

    raw_report_type = mapping.get("report_type")
    if not isinstance(raw_report_type, str):
        raise ValueError("A CRILC layout file must declare a string report_type.")
    report_type = _coerce_report_type(raw_report_type)
    if report_type is not expected_type:
        raise ValueError(
            f"Layout file report_type {report_type.value!r} does not match its filename."
        )

    raw_version = mapping.get("version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ValueError("A CRILC layout file must declare an integer version.")
    if raw_version != expected_version:
        raise ValueError(f"Layout file version {raw_version!r} does not match its filename.")

    raw_effective_from = mapping.get("effective_from")
    if not isinstance(raw_effective_from, str):
        raise ValueError("A CRILC layout file must declare a string effective_from date.")
    effective_from = date.fromisoformat(raw_effective_from)

    raw_fields = mapping.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise ValueError("A CRILC layout must declare a non-empty field list.")
    fields = tuple(_field_from_mapping(item) for item in raw_fields)
    return CrilcLayout(
        report_type=report_type,
        version=raw_version,
        effective_from=effective_from,
        fields=fields,
    )


def _field_from_mapping(raw: object) -> CrilcLayoutField:
    if not isinstance(raw, dict):
        raise ValueError("Each CRILC layout field must be a JSON object.")
    mapping: dict[str, object] = raw

    name = mapping.get("name")
    label = mapping.get("label")
    data_type = mapping.get("data_type")
    if not isinstance(name, str) or not isinstance(label, str) or not isinstance(data_type, str):
        raise ValueError("Each CRILC layout field requires string name, label and data_type.")
    max_length = mapping.get("max_length")
    if max_length is not None and (isinstance(max_length, bool) or not isinstance(max_length, int)):
        raise ValueError("A CRILC layout field's max_length must be an integer or absent.")
    return CrilcLayoutField(
        name=name,
        label=label,
        data_type=data_type,
        required=bool(mapping.get("required", True)),
        max_length=max_length,
    )


__all__ = [
    "CrilcLayoutNotFound",
    "available_layout_versions",
    "load_crilc_layout",
]
