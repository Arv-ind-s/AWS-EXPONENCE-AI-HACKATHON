# Vendored font licenses

These files are bundled so the application does not request fonts from a
third-party origin at runtime. The register is part of the release artefact;
the local design check requires every font filename to appear here.

## Source Serif 4

Files: `radar-serif.ttf`

License: SIL Open Font License 1.1 (OFL-1.1), Copyright Adobe Inc.

Source: Google Fonts `ofl/sourceserif4`, distributed from the Google Fonts
repository. The register records the applicable license and copyright notice
for the bundled file.

## IBM Plex Mono

Files: `radar-mono-regular.ttf`, `radar-mono-semibold.ttf`,
`radar-mono-bold.ttf`

License: SIL Open Font License 1.1 (OFL-1.1), Copyright IBM Corp.

Source: Google Fonts `ofl/ibmplexmono`, distributed from the Google Fonts
repository. The register records the applicable license and copyright notice
for the bundled files.

## IBM Plex Sans

Files: `radar-sans.ttf`

License: SIL Open Font License 1.1 (OFL-1.1), Copyright IBM Corp.

Source: Google Fonts `ofl/ibmplexsans`, distributed from the Google Fonts
repository. The register records the applicable license and copyright notice
for the bundled file.

## Noto Sans Devanagari

Files: `radar-devanagari.ttf`

License: SIL Open Font License 1.1 (OFL-1.1), Copyright Google LLC.

Source: Google Fonts `ofl/notosansdevanagari`, distributed from the Google
Fonts repository. The register records the applicable license and copyright
notice for the bundled file.

All bundled files are normal-font variants; no executable code is included.

## Integrity

SHA-256 digests are recorded so a release build can verify that the registered
font bytes have not changed:

```text
9ce7b04f60e363d8870e5997744cf85cf69d38a4d7d129d364d92a3b14b461d7  radar-devanagari.ttf
ac27abd6450a64dd94467580a02fe6235156d5b92f2926ebbc8e7489df64e0be  radar-mono-bold.ttf
6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46  radar-mono-regular.ttf
d3c38e55c78f5b0f28009fddba4834ec503278936a5986032424c9bd2d23aa46  radar-mono-semibold.ttf
3b031aa4216174205bd8471f88a49b91f093169e9e87bd5262242bc5967fe2e3  radar-sans.ttf
97b2d4da6e3cb494b5a1e66ae176914d852ccabef49e0c02c0df25f3e39aca0b  radar-serif.ttf
```
