# Covenant Radar demo runbook

Phase 7D rehearsal script for the local, air-gapped showcase. The run is
designed for one presenter, one browser window and a seven-to-eight-minute
walkthrough from the ranked queue to a grounded memo handoff.

## Before the room opens

Run the demo only on loopback with synthetic data. Do not upload a customer
document, paste personal data, or expose the presenter account beyond the
local machine.

From the repository root, start the prepared demo with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo_up.ps1 -LiveModel
```

The bootstrap applies migrations, loads the reference portfolio, seeds the
Phase 7A data, runs `nightly.pipeline`, creates the presenter account and
starts the server at `http://127.0.0.1:8000`. `-LiveModel` is required for
this demo: the curated roster is new for every rehearsal, so no recorded
cassette exists for its prompts, and `Generate AI explanation`/`Generate
memo` would otherwise report a provider outage instead of drafting a memo.
With `-LiveModel`, `.env`'s configured provider (`tcs`, model
`genailab-maas-gpt-5.4-mini`) drafts real memos through the corporate
gateway — billable, and requiring the corporate CA bundle
(`var/corporate-ca.pem`) to be current. Documents still use the local
encrypted store.

Use these credentials only for this disposable local demo. `riskhead` carries
every permission the golden path below touches (queue, simulator, intake,
memo generation):

```text
Username: riskhead
Password: CovenantRadar#2026
```

The first bootstrap is intended for a clean checkout. For a second rehearsal
against the same prepared database, leave the server running, sign out and
repeat the browser path in a fresh private window. Do not start a second
server on port 8000.

### Canonical records

The demo seed is a curated, 24-company roster — one company per industry,
covering eight quarters of history (`FY24Q3`–`FY26Q2`) each, deliberately
spread across five narrative tiers so the Portfolio Queue always shows at
least four companies in every band: `act` (already breached + about to
breach), `amber` (deteriorating), and `watch` (early signal + safe/stable).
Use references in the browser and the full legal names when speaking about a
record:

| Role | Borrower reference | Exact seeded legal name | First facility | Binding covenant | Story |
|---|---|---|---|---|---|
| Primary breach | `B-000004` | Arclume Textile Mills Private Limited | `F-000004-01` | `D04COV` (Interest coverage) | Already breached: coverage 1.08x vs. a 1.50x minimum threshold, in cure |
| Forecast fallback | `B-000009` | Bellora Mobility Components Private Limited | `F-000009-01` | `D09COV` (Interest coverage) | About to breach: coverage still 1.54x above threshold today, but the 90-day projection crosses it — and payment conduct is still clean (SMA band: none) |
| Second forecast fallback | `B-000017` | Cloudmere Aviation Support Private Limited | `F-000017-01` | `D17COV` (Interest coverage) | About to breach, but payment conduct is already `beyond` (90+ days past due) — conduct worsened before the covenant did |
| Amber example | `B-000013` | Cairnwell Wholesale Markets Private Limited | `F-000013-01` | `D13LIQ` (Current ratio) | Deteriorating trend, ~43% breach probability, clean conduct |
| Safe example | `B-000020` | Dalespring Software Systems Private Limited | `F-000020-01` | `D20LEV` (Leverage) | Flat/improving trend, 57% headroom, ~18% probability |

Every borrower carries all three covenant classes (leverage, interest
coverage, current ratio); the roster deliberately spreads which one binds
across companies rather than leverage always being the driver, so `Worst
covenant` genuinely varies across the queue. `B-000004` is the primary case
because its seeded interest-coverage history is already past the `D04COV`
threshold. If the latest queue ranking places a different borrower first,
search by the exact reference above rather than choosing an arbitrary live
record. The reference and facility values are synthetic identifiers, not
customer data.

### Intake document

Have a valid one-page PDF ready locally as
`var/demo/phase-7a-sanction-letter.pdf`. Its visible covenant sentence should
be exactly:

> The leverage ratio shall not exceed 3.00x.

Use a real PDF with extractable text; renaming a text file to `.pdf` will be
rejected by the document preflight. Keep the file on the presenter machine,
not in a screen-share download folder. The recorded stage-1 cassette is
offline and deterministic, but it still matches the masked clause and prompt
version, so wording changes can produce a cassette miss.

## Timed golden path

Start a stopwatch when the sign-in form is submitted. Keep the browser at
100% zoom and use the normal light theme for the first run. The dark-theme
toggle is a short optional finish after the primary path.

### 0:00–0:25 — Sign in

1. Open `http://127.0.0.1:8000/sign-in?next=%2F`.
2. Enter `riskhead` and `CovenantRadar#2026`.
3. Submit `Sign in` and wait for the redirect to `/`.

Say: “This is a local presenter session. The queue is already backed by the
completed overnight run, so the first screen is an action surface rather than
a blank dashboard.”

### 0:25–1:15 — Establish the portfolio signal

1. On `Portfolio queue`, pause on the summary strip above the filters.
2. Point to `Act now`, `Amber`, `Watch`, `Changed today` and `Portfolio
   exposure`.
3. Do not filter the queue yet. The ranked rows and their mini-trajectories
   make the risk shape visible immediately.
4. Activate the row link for `Arclume Textile Mills Private Limited`
   (`B-000004`). The row link is keyboard reachable; pressing `Enter` after
   tabbing to it is the keyboard equivalent.

Say: “The strip is portfolio-wide, while the ledger is ranked for the desk.
The mini-trajectory tells us which row deserves the next minute before we
open the case file.”

### 1:15–2:05 — Open the breach case file

1. Confirm the case-file header shows the exact primary borrower name and
   reference `B-000004`.
2. In `Covenant position`, point to the `D04COV` interest-coverage row and
   its current value, threshold, headroom and verdict, and to the "Last N
   tested quarters" trend chart beneath it — the same trajectory renderer
   used for the forward-looking forecast, fed the borrower's actual test
   history instead of a projection.
3. Point out that the page also separates evidence and source documents from
   the covenant ledger; do not edit any data during the demo.

If the primary row is not present, open `/borrowers/B-000004` directly. If
that returns the designed not-found page, use `/borrowers/B-000009` and say
“I’m moving to the next deterministic forecast case while the scope is
rechecked.” Do not use an unseeded borrower.

### 2:05–3:20 — Make the forecast the visual beat

1. Scroll to `Forecast trajectory`.
2. On the `D04COV` card, point to the stored daily trajectory, threshold
   marker and the visible crossing annotation. Name the displayed driver:
   `EBIT / finance cost`.
3. Activate the `60 days` named horizon stop. If the browser has reduced
   motion enabled, use the named stop instead of the range input; the stop is
   the supported no-JavaScript path.
4. Read the selected-day values: projected value, headroom, probability,
   confidence, crossing and drivers. Never paraphrase a suppressed
   probability as a numeric value.

Say: “This line is a persisted forecast path, not a chart invented in the
browser. Every selected-day figure remains paired with the stored value and
its confidence state.”

If a selected-day request reports an error, leave the current figures in view,
say “The last stored value remains visible while this day is unavailable,” and
continue with the three named horizon stops. If the full trajectory is absent,
move to `B-000009` and show its stored path; an empty path is a data-readiness
issue, not a reason to invent a crossing date.

### 3:20–4:05 — Open the why-panel on one figure

1. Return to the case actions and activate `Why this decision`.
2. In `Why this decision`, open the stage that contains the forecast figure
   (the stage name is rendered from the stored trace; do not rely on a guessed
   stage number).
3. Show `What it received`, `What it produced`, `Thresholds compared` and
   `Source records`.
4. Point to the observed value, threshold and comparison side for one
   leverage figure. Leave the drawer/page open long enough for the audience
   to see the rule or model version.

Use this two-sentence credibility beat verbatim:

> Every number on this page resolves to a stored record; it is not a claim
> that the model made in isolation. The panel shows the rule, the observed
> value, the threshold and the source record that let a reviewer reconstruct
> the warning.

If the why view is slow, refresh it once and keep the case file as the source
of truth. If it returns a scoped not-found response, say “The explanation
surface fails closed when its subject is outside the current scope,” then move
to `/audit` and show the available audit search rather than retrying arbitrary
IDs.

### 4:05–5:15 — Compare two interventions

1. Return to the case file and activate `Run simulation`.
2. Confirm the simulator identifies `B-000004`, its facility and the selected
   covenant forecast.
3. Select these two catalogue options:

   - `CREDIT-REDUCE-DEBT-SERVICE` — credit-led exposure and debt-service
     reduction; approval is required.
   - `RISK-REVIEW-THRESHOLD` — risk-led threshold review; approval is
     required.

   The catalogue offers options by covenant class, so the codes differ by
   covenant. `D04COV` is an interest coverage ratio (a `min` covenant), which
   is why the credit option here is `CREDIT-REDUCE-DEBT-SERVICE`. On a
   `leverage` covenant, such as `B-000002`'s `D02LEV`, the equivalent credit
   option is `CREDIT-REPAY-TERM-DEBT`. Read the codes off the screen rather
   than expecting one fixed pair.

4. Leave the parameters at their defaults and activate `Compare with
   baseline`.
5. In `Comparison against doing nothing`, show the `Do nothing (baseline)`
   column beside both selected interventions. Point to crossing date and
   probability, then to the assumptions printed below each option.

   The two options deliberately do not agree, and the contrast is the point:

   - `CREDIT-REDUCE-DEBT-SERVICE` clears the breach. The projected crossing
     leaves the horizon entirely and the probability falls from `99.00%` to
     roughly `44%` at 30 days.
   - `RISK-REVIEW-THRESHOLD` does not. It reports `0 days` and `0.00%`,
     with a printed note that the option applies but changes neither the
     projected crossing nor the probability at any stored horizon.

   That second column is a correct result, not a failure. If asked, say:
   “The threshold review is a real, applicable action, but a `0.10` relaxation
   cannot recover a covenant that is already `0.42` through its floor. The
   simulator says so rather than rounding it into an improvement.”

6. Activate `Carry selected simulations into memo generation` only after the
   comparison is visible.

Say: “The simulator does not accept an effect model from the browser. It
offers only bank-owned, applicable catalogue actions, persists each
counterfactual, and keeps the assumptions next to the result.”

If no options are shown, return to `/borrowers/B-000004` and reopen the
simulator; the financial covenant should offer the two codes above. If the
POST is rejected, read the displayed validation message, select no more than
two options and retry once. If comparison still fails, move to `Forecast
trajectory` and explain the stored baseline rather than improvising an
intervention effect.

Re-running the same comparison is safe: a simulation is write-once, so a
repeat submission returns the stored result rather than creating a second
one. Do not edit an intervention's catalogue parameters between rehearsal and
the live run — a stored simulation's content hash covers the catalogue
amount, assumptions and applicable classes, so changing any of them makes the
next run of that code fail with a `409` conflict.

### 5:15–6:35 — Run intake against the demo document

1. Open `Intake` from the main navigation, or go directly to `/intake`.
2. In `Borrower reference`, enter `B-000004`.
3. In `Facility reference`, enter `F-000004-01`.
4. Keep document type as `Sanction letter`.
5. Choose `var/demo/phase-7a-sanction-letter.pdf` and submit `Upload`.
6. Wait for the document state to reach extraction complete. Then run the
   proposal action shown for the document.
7. In the side-by-side source/proposal view, open the source span once. Show
   the proposed `leverage_ratio`, `3.00x` threshold and the six verification
   verdicts. The passing proposal must render `Confirm covenant`.
8. Do not confirm during a timed demo unless the audience specifically asks
   to see registration; confirmation is a write and is not needed to prove
   the proposal gate.

Say: “The model proposes fields from a masked clause, then code independently
checks the definition, threshold, units, dates and frequency. A failed check
is struck and cannot render a confirmation control.”

If upload is rejected, say “The upload boundary rejected the file before it
could become evidence,” show the validation message and move to the existing
case file. If extraction completes but no proposal appears, verify the exact
clause text and recorded-provider configuration; then use the intake hand-entry
form with the same clause as a controlled fallback. A provider-unavailable
state is safe and expected to preserve the rest of the workspace; it is not a
reason to claim that a model response was received.

### 6:35–7:30 — Finish at the grounded memo

1. Return to the simulator comparison and activate `Carry selected
   simulations into memo generation`.
2. In the memo workspace, review the generated headline, summary, named
   driver, selected interventions, simulated effects and assumptions.
3. Confirm the advisory statement that human credit review is required before
   action. If export is part of the presentation, choose PDF and retain the
   returned integrity hash with the committee-pack artefact.
4. End on the memo's record references, not on the generated prose.

Say: “The memo is grounded in the same forecast, evidence and simulations we
just inspected. Generated prose is marked as drafted, while the figures and
assumptions remain traceable records for human review.”

The expected handoff URL begins `/memos?simulation_ids=` and is reached from
the simulator link, so do not type simulation IDs by hand. If the link is
missing or returns a 404, treat that as a release blocker in this build: say
“The analysis is complete, but the memo web handoff is not composed in this
runtime,” move to `/audit` to show that the simulation remains recorded, and
log the missing browser memo route before presenting. Do not substitute an
untracked document or describe an unrendered memo as generated.

## Fallback card

Keep this table beside the presenter keyboard. Each fallback preserves the
truth of the product and gives the audience a visible next surface.

| If this breaks | Say | Move to |
|---|---|---|
| Sign-in loops or rejects the presenter | “The local session is not ready; I’m restarting the disposable presenter session.” | Restart the server, rerun `create_user.py` through `demo_up.ps1`, then `/sign-in`; do not bypass authentication. |
| Queue is empty or says no completed run | “This screen is correctly refusing to fabricate a ranking without a completed run.” | Terminal: `python -m radarctl job run nightly.pipeline`; refresh `/`. If it cannot complete, `/governance`. |
| `B-000004` is missing | “The borrower is outside the current portfolio scope, so the case lookup fails closed.” | `/borrowers/B-000009`, then `/borrowers/B-000017`. |
| Forecast path or selected day fails | “The previous stored value remains visible; the browser never invents a point.” | Use the `Today`, `30 days`, `60 days` and `90 days` stops, then show another seeded case. |
| Why view fails | “Explainability is scoped to the same subject as the case file.” | `/audit`, then return to the case file. |
| Simulator has no applicable action | “Only applicable, bank-owned catalogue actions can be simulated.” | `/borrowers/B-000004` → `Run simulation`; if still unavailable, show the forecast baseline. |
| Intake rejects the PDF or provider is unavailable | “The intake boundary keeps an unverified clause from becoming a covenant.” | `/intake` hand-entry with the exact clause, or the existing case file. |
| Memo link is missing/404 | “The stored analysis remains auditable, but this runtime has no browser memo handoff.” | `/audit`; record the defect and do not claim a memo was generated. |

## Presenter discipline

- Keep the browser on `127.0.0.1`; the bootstrap deliberately selects SQLite
  and local document storage. With `-LiveModel`, AI explanations and memos are
  drafted live through the corporate TCS gateway (`genailab-maas-gpt-5.4-mini`)
  — real, billable calls, not recorded playback.
- Speak from displayed values and labels. Do not turn a suppressed probability,
  unavailable crossing date or no-effect intervention into a positive claim.
- Do not expose UUIDs, database files, environment variables, request logs or
  model-call payloads on the shared screen.
- Do not use the upload or confirmation forms as a prop. They create durable,
  audited records and should be changed only when the audience asks for that
  exact write path.
- At the end, sign out with `Sign out` and stop the local server with `Ctrl+C`.

## Two-run rehearsal record

Run the complete path twice before presenting. Record the actual result here
in the commit or presenter notes so a later operator can distinguish a known
fallback from a new regression.

| Run | Date / time | Result | Fallback used | Follow-up |
|---|---|---|---|---|
| 1 |  |  |  |  |
| 2 |  |  |  |  |

The rehearsal is complete only when both runs reach the memo handoff or have
an explicitly logged memo-route blocker, and the queue, case file, forecast,
why view, simulator and intake steps have each been seen in the same browser
session.
