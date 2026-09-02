"""Stage-1 model orchestration: one masked call per candidate clause
(`spec §17.1`, `plan.md §8`'s `T-094`).

This module owns none of the parsing or normalisation logic
(`domain/intake/proposal.py`) and none of the outbound masking logic
(`ai/masking.py`); it only wires the two together through the single
guarded call site (`ai/client.py`, `C-51`): mask the candidate's clause
text, render it into the versioned `stage1_extract` template, dispatch one
call per candidate, and hand the raw reply to
:func:`covenant_radar.domain.intake.proposal.parse_stage1_reply`.

A provider failure on any one candidate propagates immediately rather than
being swallowed — the caller (the intake service) decides whether to offer
hand entry for the clauses not yet attempted, per `T-094`'s "provider
unavailable" case. Nothing here decides whether a proposal is trustworthy;
that is `T-095`'s job entirely.
"""

from __future__ import annotations

from collections.abc import Sequence

from covenant_radar.ai.client import CallContext, ModelClient
from covenant_radar.ai.masking import MaskedPrompt, build_outbound
from covenant_radar.ai.prompts.loader import DEFAULT_PROMPT_DIRECTORY, PromptFile, PromptLoader
from covenant_radar.domain.intake.candidates import ClauseCandidate
from covenant_radar.domain.intake.proposal import StageOneProposal, parse_stage1_reply

__all__ = ["COMPONENT", "PROMPT_NAME", "PROMPT_VERSION", "propose_candidates"]

PROMPT_NAME = "stage1_extract"
PROMPT_VERSION = "v1"
COMPONENT = "stage1_extraction"


def propose_candidates(
    candidates: Sequence[ClauseCandidate],
    client: ModelClient,
    *,
    prompt_loader: PromptLoader | None = None,
    request_id: str | None = None,
) -> tuple[StageOneProposal, ...]:
    """Propose stage-1 terms for every candidate, one masked call each.

    Candidates are processed in order and the result tuple mirrors that
    order exactly, so a caller can zip proposals back onto their source
    candidates without carrying an extra identifier. A provider failure on
    any call propagates immediately: the candidates already proposed are
    lost from this call's return value, which is why the caller — not this
    function — owns any partial-progress or retry policy.
    """

    if not isinstance(client, ModelClient):
        raise TypeError("propose_candidates requires a ModelClient.")
    if isinstance(candidates, str | bytes) or not isinstance(candidates, Sequence):
        raise TypeError("propose_candidates requires a sequence of ClauseCandidate values.")
    for candidate in candidates:
        if not isinstance(candidate, ClauseCandidate):
            raise TypeError("propose_candidates requires ClauseCandidate values only.")

    loader = prompt_loader or PromptLoader(DEFAULT_PROMPT_DIRECTORY)
    template = loader.load(PROMPT_NAME, PROMPT_VERSION)
    context = CallContext(request_id=request_id, component=COMPONENT)

    proposals: list[StageOneProposal] = []
    for candidate in candidates:
        prompt = _build_prompt(template, candidate)
        result = client.call(1, prompt, PROMPT_VERSION, context)
        proposals.append(parse_stage1_reply(candidate, result.text or ""))
    return tuple(proposals)


def _build_prompt(template: PromptFile, candidate: ClauseCandidate) -> MaskedPrompt:
    """Mask the candidate's clause text and render it into the template.

    Only ``clause_text`` — the document-derived free text — passes through
    masking; ``candidate_rules`` is this process's own deterministic rule
    labels (`domain/intake/candidates.py`), never text drawn from the
    document, so it carries no masking obligation of its own.
    """

    masked_clause = build_outbound({"clause_text": candidate.text}, prompt_version=PROMPT_VERSION)
    masked_clause_text = masked_clause.fields["clause_text"]
    candidate_rules = ", ".join(candidate.matched_rules)
    rendered = template.render(
        {"clause_text": masked_clause_text, "candidate_rules": candidate_rules}
    )
    return MaskedPrompt(
        content=rendered,
        version=PROMPT_VERSION,
        token_map=masked_clause.token_map,
    )
