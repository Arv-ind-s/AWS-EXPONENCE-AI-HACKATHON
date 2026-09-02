# ADR-0003: self-hosted typography and role-based theming

## Status

Accepted.

## Context

Covenant Radar presents financial evidence as dense, legible ledgers. The
interface needs three distinct typographic roles: a newspaper-style face for
headings and memo prose, a monospaced face with aligned figures for data, and a
platform-style sans for controls. Hindi and the rupee sign must remain legible
without a network request. The light and dark palettes must preserve the
meaning of the risk colours instead of treating dark mode as an inversion.

## Decision

The application bundles Source Serif 4, IBM Plex Mono, IBM Plex Sans, and Noto
Sans Devanagari under the SIL Open Font License 1.1. The font files live under
`web/static/fonts`, and the file-to-license mapping is maintained in the
adjacent license register. CSS loads them from the same origin and exposes
only the approved family stacks through `tokens.css`.

The token file is the sole source of colours, dimensions, spacing, radii,
durations and easing. The dark theme redefines each colour role explicitly.
The contrast checker measures primary text at 7:1, secondary text at 4.5:1,
risk accents at 4.5:1, and text on tinted chip surfaces at 4.5:1 for both
themes. It also rejects design literals in other stylesheets and templates,
checks the font register, and reads each font's cmap for the rupee and a
Devanagari character.

## Consequences

Fonts render consistently in an air-gapped deployment and their provenance is
reviewable with the source tree. The bundle is larger than a system-font-only
stylesheet, and a font refresh requires updating the license register and
rerunning the offline checks. Future components must consume custom
properties; a value that belongs to the visual system cannot be introduced in
component markup or styles.

## Revisit

Reconsider the family files only when the supported language set, the font
license terms, or the accessibility measurements change. Any replacement
must preserve the four typographic roles, the required glyphs, and the
contrast report.
