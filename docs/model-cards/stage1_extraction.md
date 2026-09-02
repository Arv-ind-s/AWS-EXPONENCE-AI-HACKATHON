# Model card — `stage1_extraction`

`plan.md §5.9` (`model_registration`), `spec §N-12.a`, `T-107`.

## Purpose

Proposes structured covenant terms (definition, threshold, direction,
frequency, cure/grace days) from one candidate clause of borrower-supplied
text: a sanction letter, an amendment, or a hand-entered clause. The model
never decides whether a covenant is registered — it only proposes; every
proposal is verified deterministically before a human ever sees it
(`domain/intake/proposal.py`, `spec §17.1`), and a verification failure is
refused with the specific failed check named, never silently corrected.

## Call site

`covenant_radar.ai.intake.propose_candidates` — the only caller. Reaches
the model exclusively through the single guarded call site
(`ai/client.py`, contract `C-51`); `Stage.ONE` is the only stage this
component is permitted to use.

## Prompt

`ai/prompts/stage1_extract.v1.md` — version-bound and hash-verified at
load time (`ai/prompts/loader.py`); a content change with no version bump
fails the prompt manifest check before this card's registration is even
relevant.

## Input handling

Every candidate clause is masked before it leaves the process boundary
(`ai/masking.py`) — PII and identifiers are replaced with reversible
tokens the model never sees in the clear. Reconstruction happens only on
the host, after the model reply returns.

## Output handling

The raw reply is parsed by `domain/intake/proposal.parse_stage1_reply`
into a `StageOneProposal`, then run through the full deterministic
verification pipeline (`T-095`) before any UI or API surface renders it.
An unverifiable or injection-shaped reply is refused with a named reason
and a security event, never partially trusted.

## Registration

| Field | Value |
|---|---|
| Component | `stage1_extraction` |
| Provider | Configured per deployment (`AiSettings.provider`; `none` until a provider is configured) |
| Model identifier | Configured per deployment (`AiSettings.model`) |
| Prompt version | `v1` |
| Owner | The engineering owner recorded at registration time (`ModelGovernanceService.register`) |
| Approval | Required before production use — `Permission.APPROVE_MODEL_PROMOTION` (Risk Head), distinct from the registering actor |

## Known limitations and human oversight

- The model proposes; it never registers a covenant. Every proposal still
  requires a human to confirm or correct it, and a proposal that fails
  verification cannot be confirmed by any role in any configuration
  (`spec §16.1`).
- A prompt-version bump resets this registration to `registered` and
  requires a fresh, distinct approval before production use resumes.
- Provider unavailability or a malformed reply propagates to the caller
  rather than being silently retried indefinitely (`T8` retry ceiling).
