"""Versioned, integrity-checked prompt templates.

Prompt files are application artefacts, not runtime configuration.  They are
loaded through :class:`~covenant_radar.ai.prompts.loader.PromptLoader`, which
checks the filename version, the embedded header and the committed SHA-256
manifest before returning a template to a caller.
"""

from typing import Any

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


def __getattr__(name: str) -> Any:
    """Load the implementation lazily so module execution stays warning-free."""

    if name in __all__:
        from covenant_radar.ai.prompts import loader

        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
