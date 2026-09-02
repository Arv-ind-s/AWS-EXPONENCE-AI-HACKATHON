"""Load and verify the application's versioned prompt artefacts.

The loader is intentionally independent of a model provider.  A prompt is
identified by its logical name and version (for example,
``stage1_extract``/``v1``), and is accepted only when all of the following
hold:

* its filename and first-line version header agree;
* its exact UTF-8 bytes match the checked-in SHA-256 manifest; and
* every template slot is declared by the file and, when values are supplied,
  is present in the caller's slot mapping.

The manifest is never changed as a side effect of loading or checking.  The
only write path is :func:`update_prompt_manifest`, which is exposed through
this module's explicit command-line entry point and rejects a content change
that does not carry a real version bump.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

DEFAULT_PROMPT_DIRECTORY: Final[Path] = Path(__file__).resolve().parent
DEFAULT_MANIFEST_PATH: Final[Path] = DEFAULT_PROMPT_DIRECTORY / "prompt_hashes.json"

__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_PROMPT_DIRECTORY",
    "PromptError",
    "PromptFile",
    "PromptFormatError",
    "PromptIntegrityError",
    "PromptLoader",
    "PromptManifestError",
    "PromptNotFoundError",
    "PromptPlaceholderError",
    "PromptVersionError",
    "check_prompt_manifest",
    "load_prompt",
    "main",
    "update_prompt_manifest",
    "verify_prompt_manifest",
]

_MAX_PROMPT_BYTES: Final[int] = 1 * 1024 * 1024
_PROMPT_FILENAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\.(?P<version>v[0-9]+(?:\.[0-9]+)*)\.md$"
)
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"v[0-9]+(?:\.[0-9]+)*$", re.IGNORECASE)
_VERSION_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!--\s*prompt-version\s*:\s*(?P<version>v[0-9]+(?:\.[0-9]+)*)\s*-->\s*$",
    re.IGNORECASE,
)
_SLOTS_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!--\s*prompt-slots\s*:\s*(?P<slots>[^>]*)-->\s*$", re.IGNORECASE
)
_OUTPUT_SHAPE_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!--\s*output-shape\s*:\s*(?P<shape>[A-Za-z][A-Za-z0-9_.-]*)\s*-->\s*$",
    re.IGNORECASE,
)
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\{\{\s*(?P<name>[A-Za-z][A-Za-z0-9_]*)\s*\}\}"
)
_SLOT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_]*$")


class PromptError(ValueError):
    """Base class for safe prompt loading and manifest failures."""


class PromptFormatError(PromptError):
    """Raised when a prompt file is not a valid versioned artefact."""


class PromptIntegrityError(PromptError):
    """Raised when a prompt's content differs from its recorded hash."""


class PromptManifestError(PromptError):
    """Raised when the manifest is malformed or does not cover the files."""


class PromptNotFoundError(PromptError):
    """Raised when a logical prompt name or version is not available."""


class PromptVersionError(PromptError):
    """Raised when a prompt filename and embedded version disagree."""


class PromptPlaceholderError(PromptError):
    """Raised when a prompt contains an undeclared or unfilled slot."""


@dataclass(frozen=True, slots=True)
class PromptFile:
    """A verified prompt template and its declared rendering contract."""

    name: str
    version: str
    filename: str
    source_path: Path
    content: str
    placeholders: tuple[str, ...]
    declared_slots: tuple[str, ...]
    output_shape: str

    def render(self, values: Mapping[str, object]) -> str:
        """Fill every declared template slot with caller-provided values.

        Values are inserted as text only after the prompt has passed the
        manifest check.  Missing values are refused, and unexpected values
        are refused as well so a caller cannot silently drift from the prompt
        contract.
        """

        if not isinstance(values, Mapping):
            raise TypeError("Prompt values must be a mapping.")
        supplied = _normalise_slot_names(values.keys())
        required = set(self.placeholders)
        missing = sorted(required - supplied)
        if missing:
            raise PromptPlaceholderError(
                f"Prompt {self.filename} has unfilled placeholder(s): {', '.join(missing)}."
            )
        unexpected = sorted(supplied - required)
        if unexpected:
            raise PromptPlaceholderError(
                f"Prompt {self.filename} received undeclared slot(s): {', '.join(unexpected)}."
            )
        for name in self.placeholders:
            value = values[name]
            if value is None:
                raise PromptPlaceholderError(
                    f"Prompt {self.filename} has an empty value for slot {name!r}."
                )
            if not isinstance(value, str | int | float | bool):
                raise TypeError(
                    f"Prompt slot {name!r} must be text or a scalar value; "
                    f"received {type(value).__name__}."
                )

        return _PLACEHOLDER_RE.sub(lambda match: str(values[match.group("name")]), self.content)

    @property
    def prompt_version(self) -> str:
        """Compatibility spelling used by callers at the model boundary."""

        return self.version


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    version: str
    sha256: str
    body_sha256: str | None


@dataclass(frozen=True, slots=True)
class _PromptMetadata:
    file: PromptFile
    raw_bytes: bytes
    body: str


class PromptLoader:
    """Resolve prompt names and verify them against an immutable manifest."""

    def __init__(
        self,
        prompt_directory: Path | str = DEFAULT_PROMPT_DIRECTORY,
        manifest_path: Path | str | None = None,
    ) -> None:
        self.prompt_directory = _directory_path(prompt_directory)
        self.manifest_path = Path(manifest_path or self.prompt_directory / "prompt_hashes.json")

    def load(
        self,
        name: str,
        version: str,
        *,
        slots: Iterable[str] | Mapping[str, object] | None = None,
    ) -> PromptFile:
        """Load one named/versioned prompt after verifying its full contract."""

        logical_name = _validate_name(name)
        requested_version = _validate_version(version)
        available = self._available(logical_name)
        if requested_version not in available:
            rendered = ", ".join(available) if available else "none"
            raise PromptNotFoundError(
                f"Prompt {logical_name!r} version {requested_version!r} does not exist; "
                f"available versions: {rendered}."
            )

        filename = f"{logical_name}.{requested_version}.md"
        metadata = self._read_metadata(self.prompt_directory / filename)
        manifest = self._read_manifest()
        self._verify_metadata(metadata, manifest)
        _validate_supplied_slots(metadata.file, slots)
        return metadata.file

    def verify(self) -> tuple[PromptFile, ...]:
        """Verify every prompt file and return the verified templates."""

        metadata = self._discover_metadata()
        manifest = self._read_manifest()
        discovered_names = {item.file.filename for item in metadata}
        manifest_names = set(manifest)
        missing = sorted(discovered_names - manifest_names)
        extra = sorted(manifest_names - discovered_names)
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing manifest entry for {', '.join(missing)}")
            if extra:
                parts.append(f"manifest contains absent prompt {', '.join(extra)}")
            raise PromptManifestError("Prompt hash manifest coverage failed: " + "; ".join(parts))
        for item in metadata:
            self._verify_metadata(item, manifest)
        return tuple(item.file for item in metadata)

    def update_manifest(self) -> dict[str, dict[str, str]]:
        """Write a new manifest only after an explicit, valid version bump.

        A first manifest may be created for a new prompt package.  For an
        existing manifest, a changed file with the same version is rejected,
        as is a new version whose prompt body is unchanged.  Replacing an old
        version with a changed, higher version is supported; its old entry is
        removed because the old artefact is no longer present on disk.
        """

        metadata = self._discover_metadata()
        previous = self._read_manifest(required=False)
        if previous:
            self._validate_manifest_update(metadata, previous)
        entries = {
            item.file.filename: {
                "version": item.file.version,
                "sha256": _sha256(item.raw_bytes),
                "body_sha256": _sha256(item.body.encode("utf-8")),
            }
            for item in metadata
        }
        _write_manifest_atomically(self.manifest_path, entries)
        return entries

    def _available(self, name: str) -> tuple[str, ...]:
        versions: list[str] = []
        try:
            paths = tuple(self.prompt_directory.iterdir())
        except OSError as error:
            raise PromptManifestError(
                f"Prompt directory could not be read safely: {self.prompt_directory}."
            ) from error
        for path in paths:
            if not path.is_file():
                continue
            match = _PROMPT_FILENAME_RE.fullmatch(path.name)
            if match and match.group("name") == name:
                versions.append(match.group("version").lower())
        return tuple(sorted(set(versions), key=_version_key))

    def _discover_metadata(self) -> tuple[_PromptMetadata, ...]:
        if not self.prompt_directory.is_dir():
            raise PromptManifestError(f"Prompt directory does not exist: {self.prompt_directory}.")
        try:
            paths = sorted(self.prompt_directory.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise PromptManifestError(
                f"Prompt directory could not be read safely: {self.prompt_directory}."
            ) from error
        prompt_paths = [
            path for path in paths if path.is_file() and path.suffix.casefold() == ".md"
        ]
        if not prompt_paths:
            raise PromptManifestError(f"No prompt files found in {self.prompt_directory}.")
        return tuple(self._read_metadata(path) for path in prompt_paths)

    def _read_metadata(self, path: Path) -> _PromptMetadata:
        _assert_inside_directory(path, self.prompt_directory)
        match = _PROMPT_FILENAME_RE.fullmatch(path.name)
        if match is None:
            raise PromptFormatError(f"Prompt filename {path.name!r} must be '<name>.vN.md'.")
        try:
            raw_bytes = path.read_bytes()
        except (OSError, UnicodeError) as error:
            raise PromptFormatError(f"Prompt file {path} could not be read safely.") from error
        if len(raw_bytes) > _MAX_PROMPT_BYTES:
            raise PromptFormatError(
                f"Prompt file {path.name} exceeds the {_MAX_PROMPT_BYTES}-byte limit."
            )
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PromptFormatError(f"Prompt file {path.name} is not valid UTF-8.") from error
        lines = content.splitlines()
        if not lines:
            raise PromptFormatError(f"Prompt file {path.name} is empty and has no version header.")
        header_match = _VERSION_HEADER_RE.fullmatch(lines[0])
        if header_match is None:
            raise PromptVersionError(
                f"Prompt file {path.name} has no valid first-line version header; "
                "expected '<!-- prompt-version: vN -->'."
            )
        filename_version = match.group("version").lower()
        embedded_version = header_match.group("version").lower()
        if filename_version != embedded_version:
            raise PromptVersionError(
                f"Prompt file {path.name} declares version {embedded_version!r}; "
                f"expected version {filename_version!r} from its filename."
            )

        declared_slots = _declared_slots(lines, path.name)
        placeholders = tuple(
            dict.fromkeys(match.group("name") for match in _PLACEHOLDER_RE.finditer(content))
        )
        undeclared = sorted(set(placeholders) - set(declared_slots))
        if undeclared:
            raise PromptPlaceholderError(
                f"Prompt {path.name} has unfilled placeholder(s) not supplied by its "
                f"declared slot set: {', '.join(undeclared)}."
            )
        output_shape = _output_shape(lines, path.name)
        body = "\n".join(lines[1:])
        return _PromptMetadata(
            file=PromptFile(
                name=match.group("name"),
                version=filename_version,
                filename=path.name,
                source_path=path,
                content=content,
                placeholders=placeholders,
                declared_slots=declared_slots,
                output_shape=output_shape,
            ),
            raw_bytes=raw_bytes,
            body=body,
        )

    def _read_manifest(self, *, required: bool = True) -> dict[str, _ManifestEntry]:
        try:
            raw = self.manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not required:
                return {}
            raise PromptManifestError(
                f"Prompt hash manifest is missing: {self.manifest_path}."
            ) from None
        except (OSError, UnicodeError) as error:
            raise PromptManifestError(
                f"Prompt hash manifest could not be read safely: {self.manifest_path}."
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PromptManifestError(
                f"Prompt hash manifest is not valid JSON: {self.manifest_path}."
            ) from error
        if not isinstance(payload, dict):
            raise PromptManifestError("Prompt hash manifest must contain a JSON object.")

        entries: dict[str, _ManifestEntry] = {}
        for filename, value in payload.items():
            if not isinstance(filename, str) or _PROMPT_FILENAME_RE.fullmatch(filename) is None:
                raise PromptManifestError(
                    f"Prompt hash manifest has an invalid filename: {filename!r}."
                )
            if not isinstance(value, Mapping):
                raise PromptManifestError(
                    f"Prompt hash manifest entry {filename!r} must contain version and sha256."
                )
            version = value.get("version")
            digest = value.get("sha256")
            body_digest = value.get("body_sha256")
            if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
                raise PromptManifestError(f"Manifest version for {filename} is invalid.")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise PromptManifestError(f"Manifest SHA-256 for {filename} is invalid.")
            if body_digest is not None and (
                not isinstance(body_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", body_digest) is None
            ):
                raise PromptManifestError(f"Manifest body SHA-256 for {filename} is invalid.")
            filename_version = _PROMPT_FILENAME_RE.fullmatch(filename)
            assert filename_version is not None
            if version.lower() != filename_version.group("version").lower():
                raise PromptManifestError(
                    f"Manifest version for {filename} does not match its filename."
                )
            entries[filename] = _ManifestEntry(
                version=version.lower(),
                sha256=digest.lower(),
                body_sha256=body_digest.lower() if isinstance(body_digest, str) else None,
            )
        return entries

    @staticmethod
    def _verify_metadata(
        metadata: _PromptMetadata,
        manifest: Mapping[str, _ManifestEntry],
    ) -> None:
        entry = manifest.get(metadata.file.filename)
        if entry is None:
            raise PromptManifestError(
                f"Prompt hash manifest has no entry for {metadata.file.filename}; "
                f"expected version {metadata.file.version}."
            )
        if entry.version != metadata.file.version:
            raise PromptVersionError(
                f"Prompt manifest entry for {metadata.file.filename} records version "
                f"{entry.version!r}; expected version {metadata.file.version!r}."
            )
        actual_hash = _sha256(metadata.raw_bytes)
        if actual_hash != entry.sha256:
            raise PromptIntegrityError(
                f"Prompt {metadata.file.filename} changed without a version bump; "
                f"expected version {metadata.file.version!r} and hash {entry.sha256}, "
                f"found {actual_hash}."
            )

    @staticmethod
    def _validate_manifest_update(
        metadata: Sequence[_PromptMetadata],
        previous: Mapping[str, _ManifestEntry],
    ) -> None:
        current_by_filename = {item.file.filename: item for item in metadata}
        replaced: set[str] = set()
        for item in metadata:
            current = item.file
            prior = previous.get(current.filename)
            if prior is not None:
                actual_hash = _sha256(item.raw_bytes)
                if actual_hash != prior.sha256:
                    raise PromptIntegrityError(
                        f"Prompt {current.filename} changed without a version bump; "
                        f"expected version {prior.version!r}."
                    )
                continue

            history = [
                (filename, entry)
                for filename, entry in previous.items()
                if _logical_name(filename) == current.name
            ]
            if not history:
                continue
            highest_version = max((entry.version for _, entry in history), key=_version_key)
            if _version_key(current.version) <= _version_key(highest_version):
                raise PromptVersionError(
                    f"Prompt {current.filename} requires a version bump above "
                    f"{highest_version!r} before the manifest can be updated."
                )
            for old_filename, _ in history:
                old_metadata = next(
                    (
                        candidate
                        for candidate in metadata
                        if candidate.file.filename == old_filename
                    ),
                    None,
                )
                previous_entry = previous[old_filename]
                same_body = (old_metadata is not None and old_metadata.body == item.body) or (
                    old_metadata is None
                    and previous_entry.body_sha256 is not None
                    and previous_entry.body_sha256 == _sha256(item.body.encode("utf-8"))
                )
                if same_body:
                    raise PromptIntegrityError(
                        f"Prompt {current.filename} has a version bump without a content change; "
                        f"it is identical to {old_filename}."
                    )
                if old_filename not in current_by_filename:
                    replaced.add(old_filename)

        missing = sorted(set(previous) - set(current_by_filename) - replaced)
        if missing:
            raise PromptManifestError(
                "Prompt manifest update would remove prompt(s) without a replacement: "
                + ", ".join(missing)
            )


def load_prompt(
    name: str,
    version: str,
    *,
    slots: Iterable[str] | Mapping[str, object] | None = None,
    prompt_directory: Path | str = DEFAULT_PROMPT_DIRECTORY,
    manifest_path: Path | str | None = None,
) -> PromptFile:
    """Convenience wrapper around :class:`PromptLoader.load`."""

    return PromptLoader(prompt_directory, manifest_path).load(name, version, slots=slots)


def verify_prompt_manifest(
    *,
    prompt_directory: Path | str = DEFAULT_PROMPT_DIRECTORY,
    manifest_path: Path | str | None = None,
) -> tuple[PromptFile, ...]:
    """Verify all shipped prompt files and return them in filename order."""

    return PromptLoader(prompt_directory, manifest_path).verify()


def check_prompt_manifest(
    *,
    prompt_directory: Path | str = DEFAULT_PROMPT_DIRECTORY,
    manifest_path: Path | str | None = None,
) -> None:
    """Build-check entry point; raises on any prompt or manifest defect."""

    verify_prompt_manifest(prompt_directory=prompt_directory, manifest_path=manifest_path)


def update_prompt_manifest(
    *,
    prompt_directory: Path | str = DEFAULT_PROMPT_DIRECTORY,
    manifest_path: Path | str | None = None,
) -> dict[str, dict[str, str]]:
    """Explicitly update the prompt hash manifest after validation."""

    return PromptLoader(prompt_directory, manifest_path).update_manifest()


def main(argv: Sequence[str] | None = None, *, stream: TextIO = sys.stdout) -> int:
    """Run the explicit prompt check or manifest-update command."""

    parser = argparse.ArgumentParser(prog="python -m covenant_radar.ai.prompts.loader")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("check", "update-manifest"),
        default="check",
        help="check the committed manifest or explicitly update it",
    )
    parser.add_argument("--prompt-directory", type=Path, default=DEFAULT_PROMPT_DIRECTORY)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.command == "update-manifest":
            updated_entries = update_prompt_manifest(
                prompt_directory=args.prompt_directory,
                manifest_path=args.manifest,
            )
            stream.write(f"Updated prompt hash manifest for {len(updated_entries)} prompt(s).\n")
        else:
            verified_prompts = verify_prompt_manifest(
                prompt_directory=args.prompt_directory,
                manifest_path=args.manifest,
            )
            stream.write(f"Prompt hash check passed for {len(verified_prompts)} prompt(s).\n")
    except PromptError as error:
        stream.write(f"Prompt command refused: {error}\n")
        return 2
    return 0


def _directory_path(value: Path | str) -> Path:
    directory = Path(value).resolve()
    if not directory.exists() or not directory.is_dir():
        raise PromptManifestError(f"Prompt directory does not exist: {directory}.")
    return directory


def _assert_inside_directory(path: Path, directory: Path) -> None:
    try:
        path.resolve().relative_to(directory)
    except ValueError as error:
        raise PromptFormatError(f"Prompt path escapes its package directory: {path}.") from error


def _validate_name(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value) is None:
        raise PromptNotFoundError(
            "Prompt name must be a simple logical name without path separators."
        )
    return value


def _validate_version(value: str) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise PromptVersionError("Prompt version must use the vN or vN.N format.")
    return value.lower()


def _declared_slots(lines: Sequence[str], filename: str) -> tuple[str, ...]:
    header = next((line for line in lines[1:4] if _SLOTS_HEADER_RE.fullmatch(line)), None)
    if header is None:
        raise PromptPlaceholderError(f"Prompt {filename} has no declared slot set.")
    match = _SLOTS_HEADER_RE.fullmatch(header)
    assert match is not None
    raw_slots = [slot.strip() for slot in match.group("slots").split(",") if slot.strip()]
    if not raw_slots:
        raise PromptPlaceholderError(f"Prompt {filename} declares an empty slot set.")
    if any(_SLOT_NAME_RE.fullmatch(slot) is None for slot in raw_slots):
        raise PromptPlaceholderError(f"Prompt {filename} declares an invalid slot name.")
    if len(raw_slots) != len(set(raw_slots)):
        raise PromptPlaceholderError(f"Prompt {filename} declares duplicate slots.")
    return tuple(raw_slots)


def _output_shape(lines: Sequence[str], filename: str) -> str:
    header = next((line for line in lines[1:4] if _OUTPUT_SHAPE_HEADER_RE.fullmatch(line)), None)
    if header is None:
        raise PromptFormatError(f"Prompt {filename} has no declared output shape.")
    match = _OUTPUT_SHAPE_HEADER_RE.fullmatch(header)
    assert match is not None
    return match.group("shape").lower()


def _validate_supplied_slots(
    prompt: PromptFile,
    slots: Iterable[str] | Mapping[str, object] | None,
) -> None:
    if slots is None:
        return
    supplied = _normalise_slot_names(slots.keys() if isinstance(slots, Mapping) else slots)
    missing = sorted(set(prompt.placeholders) - supplied)
    if missing:
        raise PromptPlaceholderError(
            f"Prompt {prompt.filename} has unfilled placeholder(s) not supplied by the caller: "
            f"{', '.join(missing)}."
        )
    undeclared = sorted(supplied - set(prompt.declared_slots))
    if undeclared:
        raise PromptPlaceholderError(
            f"Prompt {prompt.filename} received slot(s) outside its declared set: "
            f"{', '.join(undeclared)}."
        )


def _normalise_slot_names(values: Iterable[object]) -> set[str]:
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or _SLOT_NAME_RE.fullmatch(value) is None:
            raise PromptPlaceholderError(f"Invalid prompt slot name: {value!r}.")
        result.add(value)
    return result


def _logical_name(filename: str) -> str:
    match = _PROMPT_FILENAME_RE.fullmatch(filename)
    if match is None:
        raise PromptManifestError(f"Invalid prompt filename: {filename!r}.")
    return match.group("name")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.lower().removeprefix("v").split("."))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_manifest_atomically(path: Path, entries: Mapping[str, Mapping[str, str]]) -> None:
    parent = path.resolve().parent
    if not parent.exists() or not parent.is_dir():
        raise PromptManifestError(f"Prompt manifest directory does not exist: {parent}.")
    payload = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise PromptManifestError(f"Prompt hash manifest could not be written: {path}.") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
