# Model card — `stage7_memo`

`plan.md §5.9` (`model_registration`), `spec §N-12.a`, `T-107`.

## Purpose

Drafts the prose of a warning memo — headline, summary, cited drivers,
recommended next step, disclaimer — from a slot map of already-computed,
already-verified figures (`domain/memo/slots.py`). The model never
computes a value, a probability, a threshold comparison or a driver share;
it only writes about numbers this product already trusts, and every
number it repeats is checked against its source slot before the draft is
ever shown (`ai/shapes.py`'s stage-7 shape checks).

## Call site

`covenant_radar.ai.memo.draft_memo` — the only caller. Reaches the model
exclusively through the single guarded call site (`ai/client.py`,
contract `C-51`); `Stage.SEVEN` is the only stage this component is
permitted to use.

## Prompt

`ai/prompts/stage7_memo.v2.md` (current), `ai/prompts/stage7_memo.v1.md`
(retained for historical cassette replay only) — version-bound and
hash-verified at load time (`ai/prompts/loader.py`).

## Input handling

Every slot value that could carry document-derived free text is masked
before it leaves the process boundary (`ai/masking.py`); numeric and
categorical slots pass through as already-validated, already-scoped
figures, never as free text a person wrote.

## Output handling

The reply must match one strict JSON shape (`domain/memo` reply parsing);
every cited figure, driver name and action id must trace back to a slot or
the recommended catalogue, checked by `ai/shapes.check_stage7_shapes`
(`T6` length ceiling included). A reply that fails any shape check gets
exactly one regeneration attempt and is then refused outright
(`MemoShapeRefusal`) — the memo is never partially written, and the
returned draft is always labelled "Drafted by model" so no reader mistakes
model prose for a computed figure.

## Registration

| Field | Value |
|---|---|
| Component | `stage7_memo` |
| Provider | Configured per deployment (`AiSettings.provider`; `none` until a provider is configured) |
| Model identifier | Configured per deployment (`AiSettings.model`) |
| Prompt version | `v2` |
| Owner | The engineering owner recorded at registration time (`ModelGovernanceService.register`) |
| Approval | Required before production use — `Permission.APPROVE_MODEL_PROMOTION` (Risk Head), distinct from the registering actor |

## Known limitations and human oversight

- The model drafts prose only; it computes nothing and cites nothing that
  was not already in the slot map handed to it.
- A prompt-version bump (`v1` → `v2`, or any future bump) resets this
  registration to `registered` and requires a fresh, distinct approval
  before production use resumes.
- A memo that cannot be grounded in the available slots and catalogue
  actions is refused, never generated with a fabricated citation.
