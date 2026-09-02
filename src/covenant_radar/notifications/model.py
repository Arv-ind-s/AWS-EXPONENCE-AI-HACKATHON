"""Typed notification values and fail-closed template rendering.

Notification templates are deliberately data-only.  A template declares all
slots and their types, and rendering rejects unknown or missing required
values.  Scope-bearing values can be removed recursively before rendering;
the renderer knows which values were intentionally removed by policy and
never turns an accidentally omitted required value into a partial message.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from covenant_radar.core.errors import ValidationError
from covenant_radar.security.permissions import Permission, coerce_permission

_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")
_MAX_TEMPLATE_VERSION: Final[int] = 50
_MAX_SLOT_COUNT: Final[int] = 100
_MAX_RENDERED_SUBJECT: Final[int] = 500
_MAX_RENDERED_BODY: Final[int] = 100_000
_MISSING: Final[object] = object()


class TemplateRenderError(ValidationError):
    """A notification cannot be rendered without risking a partial send."""


class NotificationState(StrEnum):
    """Durable states used by the notification table."""

    PENDING = "pending"
    SENT = "sent"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class ScopedValue:
    """A payload value whose disclosure needs an explicit authorization check."""

    value: object
    subject_type: str | None = None
    subject_id: UUID | None = None
    required_permission: Permission | str | None = None
    portfolio_path: str | None = None

    def __post_init__(self) -> None:
        if (self.subject_type is None) != (self.subject_id is None):
            raise ValidationError(
                "ScopedValue subject_type and subject_id must be supplied together.",
                field="subject",
            )
        if self.subject_type is not None:
            if not isinstance(self.subject_type, str):
                raise ValidationError(
                    "ScopedValue subject_type must be text.", field="subject_type"
                )
            normalized_type = _bounded_name(self.subject_type, "subject_type", 50)
        else:
            normalized_type = None
        if self.subject_id is not None and not isinstance(self.subject_id, UUID):
            raise ValidationError("ScopedValue subject_id must be a UUID.", field="subject_id")
        if self.portfolio_path is not None:
            if not isinstance(self.portfolio_path, str) or not self.portfolio_path.strip():
                raise ValidationError(
                    "ScopedValue portfolio_path must be non-blank text.",
                    field="portfolio_path",
                )
            normalized_path = self.portfolio_path.strip()
        else:
            normalized_path = None
        permission = (
            None
            if self.required_permission is None
            else coerce_permission(self.required_permission)
        )
        object.__setattr__(self, "subject_type", normalized_type)
        object.__setattr__(self, "required_permission", permission)
        object.__setattr__(self, "portfolio_path", normalized_path)


ScopedContent = ScopedValue


@dataclass(frozen=True, slots=True)
class TemplateSlot:
    """One declared template input."""

    name: str
    expected_type: type[object] | tuple[type[object], ...]
    required: bool = True
    required_permission: Permission | str | None = None
    sensitive: bool = False

    def __post_init__(self) -> None:
        name = _bounded_name(self.name, "slot name", 100)
        expected = self.expected_type
        if isinstance(expected, tuple):
            if not expected or any(not isinstance(item, type) for item in expected):
                raise TypeError("TemplateSlot.expected_type must contain types.")
        elif not isinstance(expected, type):
            raise TypeError("TemplateSlot.expected_type must be a type or tuple of types.")
        if not isinstance(self.required, bool) or not isinstance(self.sensitive, bool):
            raise TypeError("TemplateSlot.required and sensitive must be booleans.")
        permission = (
            None
            if self.required_permission is None
            else coerce_permission(self.required_permission)
        )
        if self.sensitive and permission is not Permission.READ_PERSONAL_DATA:
            raise ValidationError(
                "Sensitive notification slots must require READ_PERSONAL_DATA.",
                field="required_permission",
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "required_permission", permission)


SlotSpec = TemplateSlot


@dataclass(frozen=True, slots=True)
class FilteredPayload:
    """The safe result of applying recipient disclosure policy."""

    values: Mapping[str, object]
    removed_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.values, Mapping):
            raise TypeError("FilteredPayload.values must be a mapping.")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        removed = tuple(self.removed_slots)
        if any(not isinstance(item, str) or not item for item in removed):
            raise TypeError("FilteredPayload.removed_slots must contain non-empty names.")
        object.__setattr__(self, "removed_slots", removed)

    @property
    def has_visible_content(self) -> bool:
        """Whether at least one non-empty value survived policy filtering."""

        return any(_meaningful(value) for value in self.values.values())

    @property
    def data(self) -> Mapping[str, object]:
        """Compatibility alias used by service and adapter callers."""

        return self.values


@dataclass(frozen=True, slots=True)
class RenderedNotification:
    """A validated rendering and its safe source slot values."""

    template: str
    version: str
    subject: str
    body: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class NotificationTemplate:
    """A fixed, typed notification template.

    The format language intentionally supports only simple named slots.  It
    does not evaluate expressions, attribute access, calls or user-provided
    formatters, which keeps template input from becoming executable content.
    """

    name: str
    subject_template: str
    body_template: str
    slots: tuple[TemplateSlot, ...] = ()
    version: str = "notification.v1"
    non_suppressible: bool = False

    def __post_init__(self) -> None:
        name = _bounded_name(self.name, "template name", 100)
        version = _bounded_text(self.version, "template version", _MAX_TEMPLATE_VERSION)
        if not isinstance(self.subject_template, str) or not self.subject_template.strip():
            raise ValidationError("Template subject must be non-blank text.", field="subject")
        if not isinstance(self.body_template, str) or not self.body_template.strip():
            raise ValidationError("Template body must be non-blank text.", field="body")
        if not isinstance(self.non_suppressible, bool):
            raise TypeError("NotificationTemplate.non_suppressible must be a boolean.")
        slots = tuple(self.slots)
        if not slots or len(slots) > _MAX_SLOT_COUNT:
            raise ValidationError(
                f"Template must declare between 1 and {_MAX_SLOT_COUNT} slots.",
                field="slots",
            )
        if any(not isinstance(slot, TemplateSlot) for slot in slots):
            raise TypeError("NotificationTemplate.slots must contain TemplateSlot values.")
        names = tuple(slot.name for slot in slots)
        if len(names) != len(set(names)):
            raise ValidationError("Notification template slot names must be unique.", field="slots")
        # Validate every slot reference at registration time rather than waiting
        # until a production delivery path is running.
        declared = frozenset(names)
        for source_name, source in (
            ("subject", self.subject_template),
            ("body", self.body_template),
        ):
            slot_references = _slot_references(source, source_name)
            unknown = sorted(set(slot_references) - declared)
            if unknown:
                raise ValidationError(
                    f"Template {name!r} uses undeclared slot {unknown[0]!r}.",
                    field=source_name,
                )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "slots", slots)

    @property
    def subject(self) -> str:
        """Return the source subject template."""

        return self.subject_template

    @property
    def body(self) -> str:
        """Return the source body template."""

        return self.body_template

    @property
    def slot_names(self) -> tuple[str, ...]:
        """Return declared slots in stable order."""

        return tuple(slot.name for slot in self.slots)

    def filter_payload(
        self,
        payload: Mapping[str, object],
        *,
        can_disclose: Callable[[ScopedValue], bool],
        permissions: frozenset[Permission] = frozenset(),
    ) -> FilteredPayload:
        """Remove values the recipient cannot see, failing closed.

        Unknown fields are rejected instead of being copied through.  A
        removed declared slot is tracked separately so the service can render
        the remaining body while direct callers still get a missing-slot
        error for an unfilled value.
        """

        if not isinstance(payload, Mapping):
            raise TypeError("Notification payload must be a mapping.")
        keys = tuple(payload)
        if any(not isinstance(key, str) for key in keys):
            raise TemplateRenderError("Notification payload keys must be text.", field="payload")
        unknown = sorted(set(keys) - set(self.slot_names))
        if unknown:
            raise TemplateRenderError(
                f"Notification payload contains undeclared slot {unknown[0]!r}.",
                field="payload",
            )
        declared = {slot.name: slot for slot in self.slots}
        retained: dict[str, object] = {}
        removed: list[str] = []
        for name, raw_value in payload.items():
            slot = declared[name]
            if slot.required_permission is not None and slot.required_permission not in permissions:
                removed.append(name)
                continue
            value = _filter_value(raw_value, can_disclose)
            if value is _MISSING:
                removed.append(name)
                continue
            retained[name] = value
        return FilteredPayload(retained, tuple(removed))

    def render(
        self,
        payload: Mapping[str, object] | FilteredPayload,
        *,
        allow_removed_slots: Iterable[str] = (),
    ) -> RenderedNotification:
        """Render only after checking every declared slot's type and shape."""

        if isinstance(payload, FilteredPayload):
            values = payload.values
            removed = frozenset(payload.removed_slots)
        elif isinstance(payload, Mapping):
            values = payload
            removed = frozenset()
        else:
            raise TypeError("Notification payload must be a mapping or FilteredPayload.")
        allowed_removed = removed | frozenset(allow_removed_slots)
        declared = {slot.name: slot for slot in self.slots}
        supplied = set(values)
        unknown = sorted(supplied - set(declared))
        if unknown:
            raise TemplateRenderError(
                f"Notification payload contains undeclared slot {unknown[0]!r}.",
                field="payload",
            )
        render_values: dict[str, object] = {}
        for slot in self.slots:
            if slot.name not in values:
                if slot.name in allowed_removed:
                    render_values[slot.name] = ""
                    continue
                if slot.required:
                    raise TemplateRenderError(
                        f"Notification template {self.name!r} has an unfilled slot {slot.name!r}.",
                        field=slot.name,
                    )
                render_values[slot.name] = ""
                continue
            value = _unwrap(values[slot.name])
            if not _matches(value, slot.expected_type):
                expected = _type_label(slot.expected_type)
                raise TemplateRenderError(
                    f"Notification slot {slot.name!r} must be {expected}.",
                    field=slot.name,
                )
            render_values[slot.name] = value
        subject = _render_string(self.subject_template, render_values, "subject")
        body = _render_string(self.body_template, render_values, "body")
        if len(subject) > _MAX_RENDERED_SUBJECT:
            raise TemplateRenderError("Rendered notification subject is too long.", field="subject")
        if len(body) > _MAX_RENDERED_BODY:
            raise TemplateRenderError("Rendered notification body is too long.", field="body")
        if not body.strip():
            raise TemplateRenderError("Rendered notification body is empty.", field="body")
        safe_payload = {key: _json_safe(_unwrap(value)) for key, value in values.items()}
        return RenderedNotification(self.name, self.version, subject, body, safe_payload)


@dataclass(frozen=True, slots=True)
class TemplateRegistry:
    """Immutable lookup of the templates enabled by an application build."""

    templates: tuple[NotificationTemplate, ...]

    def __post_init__(self) -> None:
        templates = tuple(self.templates)
        if any(not isinstance(template, NotificationTemplate) for template in templates):
            raise TypeError("TemplateRegistry.templates must contain NotificationTemplate values.")
        names = tuple(template.name for template in templates)
        if len(names) != len(set(names)):
            raise ValidationError("Template names must be unique.", field="templates")
        object.__setattr__(self, "templates", templates)

    def get(self, name: str) -> NotificationTemplate:
        """Return a template or refuse an unknown template."""

        normalized = _bounded_name(name, "template", 100)
        for template in self.templates:
            if template.name == normalized:
                return template
        raise TemplateRenderError(
            f"Unknown notification template {normalized!r}.", field="template"
        )

    def __getitem__(self, name: str) -> NotificationTemplate:
        return self.get(name)

    def __iter__(self) -> Iterator[NotificationTemplate]:
        return iter(self.templates)


def _filter_value(value: object, can_disclose: Callable[[ScopedValue], bool]) -> object:
    if isinstance(value, ScopedValue):
        if not can_disclose(value):
            return _MISSING
        return _filter_value(value.value, can_disclose)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TemplateRenderError("Nested notification keys must be text.", field="payload")
            filtered = _filter_value(item, can_disclose)
            if filtered is not _MISSING:
                result[key] = filtered
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        result_list: list[object] = []
        for item in value:
            filtered = _filter_value(item, can_disclose)
            if filtered is not _MISSING:
                result_list.append(filtered)
        return result_list
    return value


def _render_string(template: str, values: Mapping[str, object], field_name: str) -> str:
    result: list[str] = []
    formatter = _StringFormatter(values, field_name)
    try:
        rendered = formatter.format(template)
    except TemplateRenderError:
        raise
    except (KeyError, ValueError, TypeError) as error:
        raise TemplateRenderError(
            f"Notification {field_name} template could not be rendered.",
            field=field_name,
        ) from error
    result.append(rendered)
    return "".join(result).strip()


class _StringFormatter:
    """A formatter that permits only declared, simple names."""

    def __init__(self, values: Mapping[str, object], field_name: str) -> None:
        self.values = values
        self.field_name = field_name

    def format(self, template: str) -> str:
        import string

        output: list[str] = []
        for literal, field_name, format_spec, conversion in string.Formatter().parse(template):
            output.append(literal)
            if field_name is None:
                continue
            if not _NAME_PATTERN.fullmatch(field_name):
                raise TemplateRenderError(
                    "Notification templates allow only simple named slots.",
                    field=self.field_name,
                )
            if format_spec or conversion:
                raise TemplateRenderError(
                    "Notification templates do not allow format specifiers or conversions.",
                    field=self.field_name,
                )
            if field_name not in self.values:
                raise TemplateRenderError(
                    f"Notification slot {field_name!r} is not available.",
                    field=field_name,
                )
            output.append(_display(self.values[field_name]))
        return "".join(output)


def _display(value: object) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    return str(value)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TemplateRenderError("Notification payload contains a non-finite number.")
        return value
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_json_safe(item) for item in sorted(value, key=str)]
    raise TemplateRenderError(
        f"Notification payload contains unsupported value {type(value).__name__}."
    )


def _unwrap(value: object) -> object:
    return value.value if isinstance(value, ScopedValue) else value


def _matches(value: object, expected: type[object] | tuple[type[object], ...]) -> bool:
    if isinstance(expected, tuple):
        return any(_matches(value, item) for item in expected)
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _type_label(expected: type[object] | tuple[type[object], ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(_type_label(item) for item in expected)
    return expected.__name__


def _meaningful(value: object) -> bool:
    if value is None or value is _MISSING:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | Sequence) and not isinstance(value, str | bytes | bytearray):
        return (
            any(_meaningful(item) for item in value.values())
            if isinstance(value, Mapping)
            else any(_meaningful(item) for item in value)
        )
    return True


def _bounded_name(value: object, field_name: str, maximum: int) -> str:
    normalized = _bounded_text(value, field_name, maximum).lower()
    if not _NAME_PATTERN.fullmatch(normalized):
        raise ValidationError(f"{field_name} has an invalid format.", field=field_name)
    return normalized


def _bounded_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be text.", field=field_name)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValidationError(
            f"{field_name} must be non-blank text of at most {maximum} characters.",
            field=field_name,
        )
    if any(ord(character) < 32 and character not in "\n\r\t" for character in normalized):
        raise ValidationError(f"{field_name} contains a control character.", field=field_name)
    return normalized


def _slot_references(template: str, field_name: str) -> tuple[str, ...]:
    import string

    names: list[str] = []
    try:
        parsed = string.Formatter().parse(template)
        for _, name, format_spec, conversion in parsed:
            if name is None:
                continue
            if not _NAME_PATTERN.fullmatch(name) or format_spec or conversion:
                raise ValueError
            names.append(name)
    except ValueError as error:
        raise ValidationError(
            f"Notification {field_name} template has an invalid slot marker.",
            field=field_name,
        ) from error
    return tuple(names)


__all__ = [
    "FilteredPayload",
    "NotificationState",
    "NotificationTemplate",
    "RenderedNotification",
    "ScopedContent",
    "ScopedValue",
    "SlotSpec",
    "TemplateRegistry",
    "TemplateRenderError",
    "TemplateSlot",
]
