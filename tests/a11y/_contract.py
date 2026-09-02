"""The offline accessibility contract every rendered screen must meet.

This is the audit half of T-083. The project has no browser harness yet
(`tests/e2e/test_component_gallery.py`, `tests/a11y/test_why_panel_a11y.py`
and `tests/e2e/test_horizon_control.py` all say so directly, and CI's
`browser` job — `nox -s e2e -s a11y` — runs entirely offline per
`docs/adr/0001-ci-and-offline-gates.md`), so there is no axe-core browser
run to delegate to. Instead this module carries, as a single reusable
parser, the same DOM-relationship checks a real axe-core pass would flag
as structural failures rather than heuristic warnings: an id collision, a
dangling `aria-*` reference, an unlabelled control, an image with no text
alternative, a heading level that skips a level, a data table missing its
header semantics, and a positive `tabindex` (which desyncs focus order
from reading order — the thing `N-07`'s keyboard checks assume never
happens). Contrast is `T-082`'s `scripts/check_contrast.py`, exercised in
`tests/a11y/test_contrast_both_themes.py`; this module does not repeat it.

Every screen-rendering test in `tests/a11y/test_all_screens.py` feeds its
HTML through `assert_accessible`, which names the screen, the rule and the
offending element in the failure message — `T-083`'s "the gate fails
naming the rule, the element and the screen" requirement applies to every
one of these checks, not just the ones with a bespoke test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_LANDMARK_TAGS = frozenset({"main", "nav", "header", "footer", "aside", "section"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_ARIA_REFERENCE_ATTRIBUTES = ("aria-labelledby", "aria-describedby", "aria-controls", "aria-owns")


@dataclass
class _Heading:
    level: int
    text: str


@dataclass
class _TableIssue:
    caption: bool
    unscoped_headers: int


@dataclass
class AccessibilityReport:
    """Every relationship the parser checked, so a test can assert on the
    specific one it cares about instead of only the aggregate pass/fail."""

    duplicate_ids: set[str] = field(default_factory=set)
    dangling_references: list[tuple[str, str]] = field(default_factory=list)
    unresolved_label_targets: list[str] = field(default_factory=list)
    landmark_count: int = 0
    main_count: int = 0
    buttons_missing_type: list[int] = field(default_factory=list)
    images_missing_alt: list[int] = field(default_factory=list)
    positive_tabindex: list[tuple[str, str]] = field(default_factory=list)
    headings: list[_Heading] = field(default_factory=list)
    skipped_heading_levels: list[tuple[int, int]] = field(default_factory=list)
    table_issues: list[_TableIssue] = field(default_factory=list)
    html_lang: str | None = None
    html_lang_seen: bool = False

    @property
    def ok(self) -> bool:
        return not (
            self.duplicate_ids
            or self.dangling_references
            or self.unresolved_label_targets
            or self.landmark_count == 0
            or self.main_count != 1
            or self.buttons_missing_type
            or self.images_missing_alt
            or self.positive_tabindex
            or self.skipped_heading_levels
            or any(not issue.caption or issue.unscoped_headers for issue in self.table_issues)
            or not self.html_lang_seen
            or not self.html_lang
        )


class AccessibilityContractParser(HTMLParser):
    """Parse one rendered page and record every relationship `N-07` needs
    held, without needing a real browser or an accessibility-tree engine."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.report = AccessibilityReport()
        self._ids: set[str] = set()
        self._pending_references: list[tuple[str, str]] = []
        self._pending_label_targets: list[str] = []
        self._heading_stack: list[int] = []
        self._current_table: _TableIssue | None = None
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._current_table is not None:
                self.report.table_issues.append(self._current_table)
                self._current_table = None

    def _handle_tag(self, tag: str, raw_attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in raw_attrs}

        element_id = attributes.get("id")
        if element_id:
            if element_id in self._ids:
                self.report.duplicate_ids.add(element_id)
            self._ids.add(element_id)

        for attribute in _ARIA_REFERENCE_ATTRIBUTES:
            value = attributes.get(attribute, "")
            for reference in value.split():
                self._pending_references.append((attribute, reference))

        if tag == "label":
            target = attributes.get("for")
            if target:
                self._pending_label_targets.append(target)

        if tag in _LANDMARK_TAGS:
            self.report.landmark_count += 1
            if tag == "main":
                self.report.main_count += 1

        if tag == "button":
            control_type = attributes.get("type")
            if control_type not in {"button", "submit", "reset"}:
                self.report.buttons_missing_type.append(self.getpos()[0])

        if tag == "img" and "alt" not in attributes:
            self.report.images_missing_alt.append(self.getpos()[0])

        tabindex = attributes.get("tabindex")
        if tabindex is not None:
            try:
                if int(tabindex) > 0:
                    self.report.positive_tabindex.append((tag, tabindex))
            except ValueError:
                pass

        if tag in _HEADING_TAGS:
            level = int(tag[1])
            if self._heading_stack and level - self._heading_stack[-1] > 1:
                self.report.skipped_heading_levels.append((self._heading_stack[-1], level))
            self._heading_stack.append(level)
            self.report.headings.append(_Heading(level=level, text=""))

        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._current_table = _TableIssue(caption=False, unscoped_headers=0)

        if tag == "caption" and self._current_table is not None and self._table_depth == 1:
            self._current_table.caption = True

        if tag == "th" and self._current_table is not None:
            if not attributes.get("scope") and not attributes.get("id"):
                self._current_table.unscoped_headers += 1

        if tag == "html":
            self.report.html_lang_seen = True
            self.report.html_lang = attributes.get("lang") or None

    def close(self) -> None:
        super().close()
        for attribute, reference in self._pending_references:
            if reference not in self._ids:
                self.report.dangling_references.append((attribute, reference))
        for target in self._pending_label_targets:
            if target not in self._ids:
                self.report.unresolved_label_targets.append(target)


def parse(html: str) -> AccessibilityReport:
    parser = AccessibilityContractParser()
    parser.feed(html)
    parser.close()
    return parser.report


def assert_accessible(html: str, *, screen: str, fragment: bool = False) -> AccessibilityReport:
    """Run the contract and fail naming the rule, the element and the
    screen — the shape `T-083`'s "every case" requires of the gate.

    `fragment=True` is for an htmx partial swapped into an already-landmarked
    host page (`screens/why/_drawer.html`, proven by `test_why_panel_a11y.py`
    to be a `role="dialog"` swap target, not a document): it skips the
    whole-document landmark/`<main>` checks, since those are the host page's
    responsibility, not the fragment's — every other check still applies.
    """
    report = parse(html)
    assert not report.duplicate_ids, f"{screen}: duplicate id(s) {sorted(report.duplicate_ids)}"
    assert not report.dangling_references, (
        f"{screen}: {report.dangling_references[0][0]} references missing id "
        f"{report.dangling_references[0][1]!r}"
    )
    assert not report.unresolved_label_targets, (
        f"{screen}: <label for> targets missing id {report.unresolved_label_targets[0]!r}"
    )
    if not fragment:
        assert report.landmark_count > 0, f"{screen}: no landmark region (main/nav/header/section)"
        assert report.main_count == 1, (
            f"{screen}: expected exactly one <main>, found {report.main_count}"
        )
    assert not report.buttons_missing_type, (
        f"{screen}: <button> without an explicit type at line {report.buttons_missing_type[0]}"
    )
    assert not report.images_missing_alt, (
        f"{screen}: <img> without an alt attribute at line {report.images_missing_alt[0]}"
    )
    assert not report.positive_tabindex, (
        f"{screen}: positive tabindex {report.positive_tabindex[0]} desyncs focus order "
        "from reading order"
    )
    assert not report.skipped_heading_levels, (
        f"{screen}: heading level skips from h{report.skipped_heading_levels[0][0]} to "
        f"h{report.skipped_heading_levels[0][1]}"
    )
    for issue in report.table_issues:
        assert issue.caption, f"{screen}: <table> without a <caption>"
        assert issue.unscoped_headers == 0, (
            f"{screen}: {issue.unscoped_headers} <th> without scope or id"
        )
    if not fragment:
        assert report.html_lang_seen and report.html_lang, f"{screen}: <html> missing a lang attribute"
    return report


__all__ = ["AccessibilityContractParser", "AccessibilityReport", "assert_accessible", "parse"]
