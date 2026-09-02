"""Translation catalogue and template build-check primitives.

The shell owns the small catalogue interface used by every template.  Later
translation work can replace the built-in English catalogue with shipped
catalogue files without changing template call sites.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from types import MappingProxyType
from typing import Any

_DEFAULT_LOCALE = "en"
#: Jinja's private marker for "pass the render context as the first argument".
#: Imported lazily-by-name so a Jinja without it degrades to a locale-fixed
#: translator rather than failing to import the catalogue at all.
try:  # pragma: no cover - exercised by whichever Jinja is installed
    from jinja2.utils import _PassArg as _JinjaPassArg

    _PASS_CONTEXT: Any = _JinjaPassArg.context
except ImportError:  # pragma: no cover - defensive
    _PASS_CONTEXT = None
_TEMPLATE_SUFFIXES = frozenset({".html", ".htm", ".jinja", ".jinja2", ".j2"})
_JINJA_BLOCK = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]*>")
_USER_FACING_ATTRIBUTE = re.compile(
    r"\b(?:aria-label|aria-description|alt|placeholder|title|value)\s*=\s*"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)
_TRANSLATION_CALL = re.compile(r"\b(?:_|gettext|tr)\(\s*['\"](?P<key>[^'\"]+)['\"]\s*[,)]")


class CatalogueError(ValueError):
    """Raised when a catalogue is malformed or cannot cover a template."""


@dataclass(frozen=True, slots=True)
class LiteralString:
    """One user-facing literal found in a template source file."""

    path: Path
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Immutable locale-to-message catalogue with English fallback."""

    messages: Mapping[str, Mapping[str, str]]
    default_locale: str = _DEFAULT_LOCALE

    def __post_init__(self) -> None:
        if not self.default_locale:
            raise CatalogueError("The default locale must not be empty.")
        default_locale = self.default_locale.replace("_", "-").lower()
        normalized: dict[str, Mapping[str, str]] = {}
        for locale, entries in self.messages.items():
            if not isinstance(locale, str) or not locale:
                raise CatalogueError("Catalogue locale names must be non-empty strings.")
            normalized_locale = locale.replace("_", "-").lower()
            if not isinstance(entries, Mapping):
                raise CatalogueError(f"Catalogue entries for {locale!r} must be an object.")
            locale_messages: dict[str, str] = {}
            for key, value in entries.items():
                if not isinstance(key, str) or not key:
                    raise CatalogueError("Catalogue keys must be non-empty strings.")
                if not isinstance(value, str) or not value:
                    raise CatalogueError(f"Catalogue value for {key!r} must be non-empty text.")
                locale_messages[key] = value
            normalized[normalized_locale] = MappingProxyType(locale_messages)
        if default_locale not in normalized:
            raise CatalogueError(
                f"Catalogue is missing its default locale {self.default_locale!r}."
            )
        object.__setattr__(self, "messages", MappingProxyType(normalized))
        object.__setattr__(self, "default_locale", default_locale)

    def has(self, key: str, *, locale: str | None = None) -> bool:
        """Return whether *key* exists in the requested locale or fallback."""
        selected = _locale_chain(locale or self.default_locale, self.default_locale)
        return any(key in self.messages.get(candidate, {}) for candidate in selected)

    def translate(self, key: str, *, locale: str | None = None, **values: object) -> str:
        """Translate one key, falling back to English and exposing missing keys."""
        if not isinstance(key, str) or not key.strip():
            raise CatalogueError("Translation keys must be non-empty strings.")
        selected = _locale_chain(locale or self.default_locale, self.default_locale)
        message = next(
            (
                self.messages[candidate][key]
                for candidate in selected
                if key in self.messages.get(candidate, {})
            ),
            None,
        )
        if message is None:
            # A visible marker is safer than silently shipping an untranslated
            # key.  The build check remains the enforcement mechanism.
            return f"⟦{key}⟧"
        try:
            return message.format_map(_SafeFormatValues(values))
        except KeyError as error:
            raise CatalogueError(
                f"Translation {key!r} requires missing value {error.args[0]!r}."
            ) from error


class _SafeFormatValues(dict[str, object]):
    """Format-map adapter that rejects implicit access to absent variables."""

    def __missing__(self, key: str) -> object:
        raise KeyError(key)


class Translator:
    """Callable template translator bound to one locale and catalogue."""

    def __init__(self, catalogue: Catalogue, *, locale: str = _DEFAULT_LOCALE) -> None:
        self.catalogue = catalogue
        self.locale = locale

    def __call__(self, key: str, **values: object) -> str:
        """Translate *key* for this translator's locale."""
        return self.catalogue.translate(key, locale=self.locale, **values)

    gettext = __call__


class ContextTranslator(Translator):
    """Template translator that follows the locale of the render context.

    Every screen already puts the request's locale into its template context —
    `base.html` uses it for `<html lang>` — but ``_`` was bound to a single
    locale at application startup, so switching language changed the language
    attribute and nothing else.  Resolving per render is what makes the
    shipped Hindi catalogue reachable.  A context with no locale (the
    component gallery, offline audit renders) falls back to the bound default,
    so nothing that worked before changes.
    """

    #: Jinja only passes the render context to a global that asks for it.
    jinja_pass_arg = _PASS_CONTEXT

    def __call__(self, context: object, key: str, **values: object) -> str:  # type: ignore[override]
        """Translate *key* for the locale carried by *context*."""
        locale = context.get("locale") if hasattr(context, "get") else None
        if not isinstance(locale, str) or not locale.strip():
            locale = self.locale
        return self.catalogue.translate(key, locale=locale, **values)

    gettext = __call__


#: Hindi for the application shell only.  Anything absent here falls back to
#: English through the catalogue's locale chain, which is deliberate: a
#: machine-guessed Hindi threshold or verdict is a number a credit officer
#: might act on, and this catalogue does not invent those.
_HINDI_SHELL_MESSAGES: dict[str, str] = {
    "navigation.label": "मुख्य नेविगेशन",
    "navigation.queue": "पोर्टफोलियो कतार",
    "navigation.cases": "मामले",
    "navigation.borrowers": "उधारकर्ता",
    "navigation.facilities": "ऋण सुविधाएँ",
    "navigation.portfolios": "पोर्टफोलियो",
    "navigation.covenants": "अनुबंध शर्तें",
    "navigation.certificates": "प्रमाणपत्र",
    "navigation.intake": "दस्तावेज़ प्रविष्टि",
    "navigation.financial_statements": "वित्तीय विवरण",
    "navigation.document_review": "दस्तावेज़ समीक्षा",
    "navigation.simulator": "सिम्युलेटर",
    "navigation.audit": "ऑडिट अभिलेख",
    "navigation.governance": "गवर्नेंस",
    "navigation.catalogue": "कार्रवाई सूची",
    "navigation.operations": "परिचालन",
    "navigation.configuration": "विन्यास",
    "navigation.admin": "प्रशासन",
    "navigation.admin_users": "उपयोगकर्ता और पहुँच",
    "navigation.notifications": "सूचनाएँ",
    "navigation.notifications_unread": "{count} अपठित सूचनाएँ",
    "navigation.search": "खोज",
    "navigation.sign_in": "साइन इन",
    "navigation.sign_out": "साइन आउट",
    "navigation.group_monitor": "निगरानी",
    "navigation.group_data": "पोर्टफोलियो डेटा",
    "navigation.group_workflows": "कार्यप्रवाह",
    "navigation.group_admin": "प्रशासन",
    "navigation.theme_dark": "गहरा थीम प्रयोग करें",
    "navigation.theme_light": "हल्का थीम प्रयोग करें",
    "navigation.language_english": "अंग्रेज़ी में बदलें",
    "navigation.language_hindi": "हिंदी में बदलें",
    "navigation.language_english_short": "EN",
    "navigation.language_hindi_short": "हिं",
    "shell.skip_to_content": "मुख्य सामग्री पर जाएँ",
    "shell.navigation": "कार्यक्षेत्र नेविगेशन",
    "shell.close_navigation": "नेविगेशन बंद करें",
    "shell.open_navigation": "नेविगेशन खोलें",
    "shell.collapse_sidebar": "साइडबार संक्षिप्त करें",
    "shell.workspace": "कार्यक्षेत्र",
    "shell.current_view": "वर्तमान दृश्य",
    "shell.search_label": "कार्यक्षेत्र में खोजें",
    "shell.search_placeholder": "उधारकर्ता, मामले, अनुबंध शर्तें खोजें...",
    "shell.open_user_menu": "उपयोगकर्ता मेन्यू खोलें",
    "shell.signed_in_user": "साइन-इन उपयोगकर्ता",
    "shell.secure_workspace": "सुरक्षित कार्यक्षेत्र",
    "shell.change_password": "पासवर्ड बदलें",
    "shell.decision_workspace": "निर्णय कार्यक्षेत्र",
}

_DEFAULT_MESSAGES: dict[str, str] = {
    "app.name": "Covenant Radar",
    "app.wordmark_primary": "Covenant",
    "app.wordmark_secondary": "Radar",
    "navigation.label": "Primary navigation",
    "navigation.queue": "Portfolio queue",
    "navigation.cases": "Cases",
    "navigation.borrowers": "Borrowers",
    "navigation.admin": "Administration",
    "navigation.admin_users": "Users & access",
    "navigation.sign_out": "Sign out",
    "navigation.sign_in": "Sign in",
    "navigation.notifications": "Notifications",
    "navigation.notifications_unread": "{count} unread notifications",
    "navigation.search": "Search",
    "navigation.certificates": "Certificates",
    "navigation.document_review": "Document review",
    "navigation.catalogue": "Action catalogue",
    "navigation.operations": "Operations",
    "navigation.configuration": "Configuration",
    "navigation.group_monitor": "Monitor",
    "navigation.group_data": "Portfolio data",
    "navigation.group_workflows": "Workflows",
    "navigation.group_admin": "Administration",
    "shell.skip_to_content": "Skip to content",
    "shell.navigation": "Workspace navigation",
    "shell.close_navigation": "Close navigation",
    "shell.open_navigation": "Open navigation",
    "shell.collapse_sidebar": "Collapse sidebar",
    "shell.workspace": "Workspace",
    "shell.current_view": "Current view",
    "shell.search_label": "Search the workspace",
    "shell.search_placeholder": "Search borrowers, cases, covenants...",
    "shell.open_user_menu": "Open user menu",
    "shell.signed_in_user": "Signed-in user",
    "shell.secure_workspace": "Secure workspace",
    "shell.change_password": "Change password",
    "shell.decision_workspace": "Decision workspace",
    "live.status": "Live workspace",
    "live.connected": "Live updates connected",
    "live.activity": "Live activity",
    "live.close_activity": "Close live activity",
    "live.empty": "No new activity in this workspace.",
    "live.updated": "Live activity updated",
    "auth.product_introduction": "Product introduction",
    "auth.eyebrow": "Evidence-backed early warning",
    "auth.story_title": "See covenant pressure before it becomes a breach.",
    "auth.story_message": (
        "One secure workspace for monitoring, evidence, forecasts, and auditable decisions."
    ),
    "auth.secure_access": "Secure access",
    "error.404.title": "Page not found",
    "error.404.heading": "This page is not available",
    "error.404.message": "The address may be out of date or the page may have moved.",
    "error.404.action": "Return to the portfolio queue",
    "error.500.title": "Something went wrong",
    "error.500.heading": "We could not complete that request",
    "error.500.message": "The incident has been recorded. Contact support with this reference:",
    "error.500.reference": "Support reference: {reference}",
    "error.500.action": "Return to the portfolio queue",
    "error.400.title": "Request could not be completed",
    "error.401.title": "Sign-in required",
    "error.403.title": "Access denied",
    "error.404.generic": "The requested resource was not found.",
    "error.409.title": "The record changed",
    "error.422.title": "The request needs correction",
    "error.503.title": "Service temporarily unavailable",
    "navigation.facilities": "Facilities",
    "navigation.portfolios": "Portfolios",
    "navigation.covenants": "Covenants",
    "navigation.intake": "Intake",
    "navigation.financial_statements": "Financial statements",
    "navigation.simulator": "Simulator",
    "navigation.audit": "Audit trail",
    "navigation.governance": "Governance",
    "navigation.theme_dark": "Use dark theme",
    "navigation.theme_light": "Use light theme",
    "navigation.language_english": "Switch to English",
    "navigation.language_hindi": "Switch to Hindi",
    "navigation.language_english_short": "EN",
    "navigation.language_hindi_short": "हिं",
    "master.common.actions": "Actions",
    "master.common.open": "Open",
    "master.common.active": "Present",
    "master.common.inactive": "Not present",
    "master.common.save": "Save changes",
    "master.common.deactivate": "Deactivate",
    "master.common.version": "Version",
    "master.common.form_error": "The form needs correction",
    "master.common.required": "Required",
    "master.common.field": "Field",
    "master.common.value": "Value",
    "master.borrowers.title": "Borrowers",
    "master.borrowers.heading": "Borrower master data",
    "master.borrowers.new": "Add borrower",
    "master.borrowers.empty": "No borrowers are available in this scope.",
    "master.borrowers.reference": "Reference",
    "master.borrowers.legal_name": "Legal name",
    "master.borrowers.portfolio": "Portfolio",
    "master.borrowers.status": "Status",
    "master.borrowers.filter_search": "Search borrowers",
    "master.borrowers.filter_search_placeholder": "Reference or legal name",
    "master.borrowers.filter_all_portfolios": "All portfolios",
    "master.borrowers.filter_all_statuses": "All statuses",
    "master.borrowers.filter_active": "Active",
    "master.borrowers.filter_inactive": "Inactive",
    "master.borrowers.filter_apply": "Apply filters",
    "master.borrowers.filter_clear": "Clear filters",
    "master.borrowers.no_matches": "No borrowers match the active filters.",
    "master.borrower.title": "Borrower",
    "master.borrower.heading": "Borrower details",
    "master.borrower.cin_present": "CIN on file",
    "master.facilities.title": "Facilities",
    "master.facilities.heading": "Facility master data",
    "master.facilities.new": "Add facility",
    "master.facilities.empty": "No facilities are available in this scope.",
    "master.facilities.reference": "Reference",
    "master.facilities.borrower": "Borrower",
    "master.facilities.type": "Facility type",
    "master.facilities.limit": "Sanctioned limit",
    "master.facilities.currency": "Currency",
    "master.facilities.sanction_date": "Sanction date",
    "master.facilities.effective": "Effective from",
    "master.facilities.maturity": "Maturity date",
    "master.facilities.effective_to": "Effective to",
    "master.facilities.outstanding": "Outstanding",
    "master.facilities.utilisation": "Utilisation",
    "master.facilities.status": "Version status",
    "master.facilities.amount_unit": "Amounts in ₹ crore",
    "master.facilities.filter_search": "Search facilities",
    "master.facilities.filter_search_placeholder": "Facility reference, borrower reference or name",
    "master.facilities.filter_all_types": "All facility types",
    "master.facilities.filter_all_currencies": "All currencies",
    "master.facilities.filter_status_current": "Current versions",
    "master.facilities.filter_status_superseded": "Superseded versions",
    "master.facilities.filter_status_all": "All versions",
    "master.facilities.filter_apply": "Apply filters",
    "master.facilities.no_matches": "No facilities match the active filters.",
    "master.facilities.insights_link": "Book insights",
    "master.facilities.result_summary": "Showing {first}–{last} of {total} facilities",
    "master.facilities.result_empty": "Nothing to show for the active filters.",
    "master.insights.title": "Facility book insights",
    "master.insights.heading": "Facility book insights",
    "master.insights.intro": (
        "Everything the master data already holds about the sanctioned book, "
        "summarised: how much is lent, how hard it is worked, when it was "
        "sanctioned and when it falls due."
    ),
    "master.insights.empty": "There are no facilities in this scope to summarise.",
    "master.insights.headline": "Headline figures",
    "master.insights.bucket": "Bucket",
    "master.insights.count": "Facilities",
    "master.insights.share": "Share of book",
    "master.insights.chart": "Relative size",
    "master.insights.truncated": (
        "This summary covers the first {limit} facilities in scope, not the whole book."
    ),
    "master.insights.back": "Back to facilities",
    "master.facility.title": "Facility",
    "master.facility.heading": "Facility details",
    "master.facility.record": "Facility record",
    "master.facility.history": "Limit history",
    "master.facility.history_intro": (
        "A limit change never overwrites a facility: it closes the current "
        "version and opens a successor, so every revision stays readable here."
    ),
    "master.facility.history_change": "Change from previous version",
    "master.facility.history_empty": "This facility has no earlier or later versions.",
    "master.portfolios.title": "Portfolios",
    "master.portfolios.heading": "Portfolio master data",
    "master.portfolios.new": "Add portfolio",
    "master.portfolios.empty": "No portfolios are available in this scope.",
    "master.portfolios.code": "Portfolio code",
    "master.portfolios.name": "Portfolio name",
    "master.portfolios.branch": "Branch code",
    "master.portfolio.title": "Portfolio",
    "master.portfolio.heading": "Portfolio details",
    "ratio.reason.missing_line": "Missing required statement line(s): {names}.",
    "ratio.reason.zero_denominator": "{denominator} is zero.",
    "ratio.reason.sign_meaningless_denominator": "{denominator} is not positive ({value}).",
    "ratio.reason.period_incomplete": (
        "The statement period failed a balance-sheet or profit-and-loss identity check "
        "and is not a sound basis for this ratio."
    ),
    "ratio.reason.facility_facts_absent": "Missing required facility fact(s): {names}.",
    "ratio.reason.formula_not_computable": "This definition's formula could not produce a value.",
}


def default_catalogue() -> Catalogue:
    """Return the built-in catalogue: full English, Hindi for the shell.

    Hindi covers the application shell — navigation, the top bar, the user
    menu — which is what the language switcher visibly changes.  Screen bodies
    fall back to English by the catalogue's own locale chain rather than being
    machine-translated into text a credit officer would then act on.
    """
    return Catalogue({_DEFAULT_LOCALE: _DEFAULT_MESSAGES, "hi": _HINDI_SHELL_MESSAGES})


def load_catalogue(source: Path | str | Mapping[str, Any] | None = None) -> Catalogue:
    """Load a catalogue from JSON, a mapping, or the built-in English scaffold.

    JSON may be either ``{"key": "value"}`` for English or
    ``{"en": {"key": "value"}, "hi": {...}}`` for multiple locales.
    """
    if source is None:
        return default_catalogue()
    if isinstance(source, Mapping):
        return _catalogue_from_mapping(source)
    path = Path(source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CatalogueError(f"Catalogue cannot be read: {path}") from error
    except json.JSONDecodeError as error:
        raise CatalogueError(f"Catalogue is not valid JSON: {path}: {error}") from error
    if not isinstance(payload, Mapping):
        raise CatalogueError(f"Catalogue root must be an object: {path}")
    return _catalogue_from_mapping(payload)


def _catalogue_from_mapping(source: Mapping[str, Any]) -> Catalogue:
    if not source:
        raise CatalogueError("Catalogue must contain at least one message.")
    if all(isinstance(value, str) for value in source.values()):
        return Catalogue({_DEFAULT_LOCALE: dict(source)})
    if not all(isinstance(value, Mapping) for value in source.values()):
        raise CatalogueError("Catalogue must contain either messages or locale objects.")
    return Catalogue({str(locale): dict(entries) for locale, entries in source.items()})


def translator_for(
    catalogue: Catalogue | None = None, *, locale: str = _DEFAULT_LOCALE
) -> Translator:
    """Build the callable installed as ``_`` in every Jinja environment.

    Returns the context-following translator where Jinja supports it, so the
    locale a screen renders under is the locale the user selected, and the
    fixed-locale translator otherwise.
    """
    resolved = catalogue or default_catalogue()
    if _PASS_CONTEXT is not None:
        return ContextTranslator(resolved, locale=locale)
    return Translator(resolved, locale=locale)


def find_template_literals(template_directory: Path | str) -> tuple[LiteralString, ...]:
    """Find static user-facing text and label attributes in template files."""
    directory = Path(template_directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Template directory does not exist: {directory}")
    findings: list[LiteralString] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in _TEMPLATE_SUFFIXES:
            findings.extend(
                find_template_literals_in_text(path.read_text(encoding="utf-8"), path=path)
            )
    return tuple(findings)


def find_template_literals_in_text(
    source: str, *, path: Path | str = "<template>"
) -> tuple[LiteralString, ...]:
    """Find user-facing literals while preserving their source line numbers."""
    template_path = Path(path)
    sanitized = _SCRIPT_OR_STYLE.sub("", _HTML_COMMENT.sub("", _JINJA_BLOCK.sub("", source)))
    findings: list[LiteralString] = []
    for line_number, line in enumerate(sanitized.splitlines(), start=1):
        text = unescape(_HTML_TAG.sub("", line)).strip()
        if _looks_user_facing(text):
            findings.append(LiteralString(template_path, line_number, text))
        for match in _USER_FACING_ATTRIBUTE.finditer(line):
            value = unescape(match.group("value")).strip()
            if _looks_user_facing(value):
                findings.append(LiteralString(template_path, line_number, value))
    return tuple(findings)


def assert_no_literal_user_facing_strings(template_directory: Path | str) -> None:
    """Fail the build with every offending template path and line number."""
    findings = find_template_literals(template_directory)
    if not findings:
        return
    details = ", ".join(f"{item.path}:{item.line} ({item.text})" for item in findings)
    raise CatalogueError(f"Literal user-facing template string(s) are forbidden: {details}.")


def find_template_translation_keys(template_directory: Path | str) -> tuple[str, ...]:
    """Return distinct statically declared translation keys in templates."""
    directory = Path(template_directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"Template directory does not exist: {directory}")
    keys: set[str] = set()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in _TEMPLATE_SUFFIXES:
            keys.update(
                match.group("key")
                for match in _TRANSLATION_CALL.finditer(path.read_text(encoding="utf-8"))
            )
    return tuple(sorted(keys))


def assert_catalogue_covers_templates(
    template_directory: Path | str, catalogue: Catalogue, *, locale: str | None = None
) -> None:
    """Fail when a template references a key absent from its catalogue."""
    missing = [
        key
        for key in find_template_translation_keys(template_directory)
        if not catalogue.has(key, locale=locale)
    ]
    if missing:
        raise CatalogueError(
            "Template translation key(s) missing from catalogue: " + ", ".join(missing)
        )


def _locale_chain(locale: str, default_locale: str) -> tuple[str, ...]:
    normalized = locale.replace("_", "-").lower()
    candidates = [normalized]
    base = normalized.split("-", maxsplit=1)[0]
    if base not in candidates:
        candidates.append(base)
    if default_locale.lower() not in candidates:
        candidates.append(default_locale.lower())
    return tuple(candidates)


def _looks_user_facing(value: str) -> bool:
    return bool(value and re.search(r"[\w\d]", value, flags=re.UNICODE))


# Friendly aliases used by build scripts and future tasks.
check_template_literals = assert_no_literal_user_facing_strings
assert_no_literal_strings = assert_no_literal_user_facing_strings
load_catalogues = load_catalogue
load_catalog = load_catalogue
check_template_i18n = assert_no_literal_user_facing_strings


__all__ = [
    "Catalogue",
    "CatalogueError",
    "ContextTranslator",
    "LiteralString",
    "Translator",
    "assert_catalogue_covers_templates",
    "assert_no_literal_strings",
    "assert_no_literal_user_facing_strings",
    "check_template_literals",
    "check_template_i18n",
    "default_catalogue",
    "find_template_literals",
    "find_template_literals_in_text",
    "find_template_translation_keys",
    "load_catalogue",
    "load_catalog",
    "load_catalogues",
    "translator_for",
]
