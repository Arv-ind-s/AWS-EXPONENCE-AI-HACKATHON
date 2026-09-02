"""The built-in content scanner every upload is checked against.

`UploadGuard` is fail-closed: it refuses to store bytes that no scanner has
looked at.  Before this module existed the only scanner was one a deployment
had to inject, so the composition root that forgot to inject one turned that
fail-closed stance into a total upload outage — every upload refused with
"virus scanning is not configured", with no configuration that could make it
pass.

The fix is not to weaken the gate but to give it a real default.
:class:`ContentSignatureScanner` needs no daemon, no network and no external
package: it rejects the bytes that must never reach a document store no matter
what a deployment has installed — the EICAR test file, native executables
smuggled behind a document extension, and OOXML packages carrying a macro
part.  It is deliberately a floor, not a replacement for a licensed engine: a
deployment that has one still injects it and it is used instead.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass

from covenant_radar.security.uploads import ScanResult

#: The industry-standard antivirus test string (not malware).  Any real engine
#: flags it, so keeping it here means the built-in scanner can be demonstrated
#: and regression-tested exactly like a licensed one.
EICAR_SIGNATURE = (
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)

#: Leading bytes of formats that are executable code rather than documents.
#: A document store must never accept one, whatever extension it arrived under.
_EXECUTABLE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "DOS/Windows executable header"),
    (b"\x7fELF", "ELF executable header"),
    (b"\xca\xfe\xba\xbe", "Mach-O fat binary header"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable header"),
    (b"#!", "script shebang"),
)

#: Parts an OOXML package only carries when it contains executable macros.
_MACRO_PARTS: tuple[str, ...] = (
    "word/vbaProject.bin",
    "xl/vbaProject.bin",
    "ppt/vbaProject.bin",
)

_ENGINE_NAME = "covenant-radar-content-signature/v1"
_MAX_ARCHIVE_ENTRIES = 1_000


@dataclass(frozen=True, slots=True)
class ContentSignatureScanner:
    """Reject known-bad content by signature, without an external engine.

    Callable so it satisfies the `VirusScanner` protocol directly.
    """

    engine: str = _ENGINE_NAME
    reject_macros: bool = True

    def __call__(self, content: bytes) -> ScanResult:
        """Return a clean or unclean result for one upload's bytes."""
        if not isinstance(content, bytes | bytearray | memoryview):
            raise TypeError("An upload scanner receives the upload's bytes.")
        data = bytes(content)
        reason = self._first_reason(data)
        if reason is not None:
            return ScanResult(clean=False, engine=self.engine, reason=reason)
        return ScanResult(clean=True, engine=self.engine)

    scan = __call__

    def _first_reason(self, data: bytes) -> str | None:
        if EICAR_SIGNATURE in data:
            return "the EICAR antivirus test signature is present"
        for magic, description in _EXECUTABLE_MAGIC:
            if data.startswith(magic):
                return f"the file begins with a {description}"
        if self.reject_macros and data.startswith(b"PK\x03\x04"):
            macro_part = _macro_part(data)
            if macro_part is not None:
                return f"the package carries the macro part {macro_part}"
        return None


def _macro_part(data: bytes) -> str | None:
    """Name the first macro part in an OOXML package, if it carries one."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names: Sequence[str] = archive.namelist()[:_MAX_ARCHIVE_ENTRIES]
    except (OSError, ValueError, zipfile.BadZipFile):
        # A package this scanner cannot open is not cleared by this check;
        # `UploadGuard` has already refused anything that is not a recognised
        # document type, so there is nothing further to decide here.
        return None
    for name in names:
        if name in _MACRO_PARTS:
            return name
    return None


def default_upload_scanner() -> ContentSignatureScanner:
    """The scanner `UploadGuard` uses when a deployment injects none."""
    return ContentSignatureScanner()


__all__ = [
    "EICAR_SIGNATURE",
    "ContentSignatureScanner",
    "default_upload_scanner",
]
