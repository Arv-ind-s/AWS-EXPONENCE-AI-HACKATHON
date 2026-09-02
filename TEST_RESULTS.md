# Covenant Radar — manual QA pass, 2026-09-01

**Tester:** live-browser QA pass (Playwright MCP) against the running app, no product code changed.
**Scope:** `spec.md §9` flow F-01 (morning triage) end to end, `spec.md §22`'s evaluation walkthrough, `spec.md §19`'s failure cases, `plan.md §7`'s viewports/accessibility floor, and the screen tasks in `tasks.md §5` (`T-073` queue, `T-075`–`T-078` case file, `T-079` simulator, `T-097` intake, `T-099`–`T-102` memo).
**Verdict: not safe to demo.** F-01's own scripted climax — generate the memo, run an intervention — cannot be completed on this build, and the UI does not visually match `spec.md §15`'s design direction on any screen.

This file records what was found in enough detail (exact repro steps, DOM/CSS evidence, console text, and file:line root causes) to write a fix plan directly from it. Findings are ranked worst first.

---

## Environment this pass ran against

- App started via the existing `var/covenant-radar.db` (already seeded: 5,000 reference-portfolio borrowers + 108 Phase 7A demo covenants, forecasts current as of 2026-09-01), served with `python -m radarctl serve` under the hermetic demo env vars from `scripts/demo_up.ps1`'s `Set-DemoEnvironment` (`COVENANT_RADAR_AI__PROVIDER=recorded`, `COVENANT_RADAR_DOCUMENTS__STORE=local`, `COVENANT_RADAR_DATABASE__URL=sqlite:///var/covenant-radar.db`), on `http://127.0.0.1:8000`.
- Signed in as the `riskhead` persona (`risk_head` role — portfolio-wide view, simulator, memos, overrides), password `CovenantRadar#2026`, per `create_user.py`.
- Borrowers exercised: **B-000008** "Deshmukh Precision Materials and Components Kavya 00008 Private Limited" (top of queue, Act band) and **B-000006** "Patel Applied Engineering Systems Aarav 00006 Private Limited" (Act band).
- The database was backed up before any state-changing action and restored afterward (see **Cleanup** at the end); row counts verified identical before/after.

---

## Findings

### 1. [BREAKS THE DEMO] "Prepare memo" 404s from every entry point — memo generation is unreachable

- **Where:** Case file → Case actions → *Prepare memo* (any borrower). Also the Simulator's own results panel links to the same dead URL.
- **Repro:** Sign in → open `/borrowers/B-000008` (or `B-000006`) → click **Prepare memo** in the "Case actions" panel.
- **Result:** Navigates to `/memos?borrower_id=<uuid>` → **HTTP 404**, the designed 404 page ("This page is not available" / "The address may be out of date or the page may have moved."). Confirmed on both B-000008 (`/memos?borrower_id=018cf0ae-3d48-7449-aecf-15a96bfb6535`) and B-000006 (`/memos?borrower_id=018cecbf-28c6-725e-b944-4ad9a012171b`) — repeatable, not a flake.
- **Root cause:** `src/covenant_radar/web/view_models/borrower.py:838` builds `memo_href = f"/memos?borrower_id={borrower.id}"`, and `src/covenant_radar/web/view_models/simulation.py:493-494` builds a similar `/memos?...` href for the simulator's results panel — but **no web screen route exists for it**. `src/covenant_radar/web/routes/` has no `memos.py` (checked directory listing directly). The only `create_memos_router` in the codebase is `src/covenant_radar/api/v1/routers/memos.py`, registered in `web/application.py:407` under the **JSON API** namespace, not the browser UI.
- **Planning gap, not just a bug:** `plan.md §7.4`'s screens table (Portfolio queue, Borrower case file, Covenant intake, Intervention simulator, Audit and reconstruction, Governance, Cases, Administration, Sign-in) **has no "Memo" row**. `T-099`–`T-102` build the memo domain logic (slot assembly, stage-7 drafting, refusal/retry, PDF/DOCX export) but none of them is a screen task, and no other task appears to own a memo *screen*. `T-078` ("evidence margin, document strip and **case actions**") is what added the "Prepare memo" link — its target was never built by anything.
- **Fix direction:** either (a) add the missing web screen/route (new task, since none currently owns it), or (b) if memo preparation was meant to happen inline (a drawer on the case file, matching the drawer-only-shadow rule in `spec.md §15.3`), point `actions_memo`'s href at that instead of a nonexistent page. Either way this needs a task added to the backlog — it isn't just a one-line fix inside an existing task's `Files owned`.

### 2. [BREAKS THE DEMO] Intervention simulator has zero applicable interventions for every covenant tested

- **Where:** Case file → Case actions → *Run simulation* (any borrower's binding covenant).
- **Repro:** From `/borrowers/B-000008`, click **Run simulation** on the "lev" covenant (`D08LEV`, threshold 3.00x, 99% probability, crossing 01 Oct 2026) → lands on `/simulator/01a05bb1-16b2-72ee-a913-09a8aeb6f4f9`. Repeated on B-000006's covenant → `/simulator/01a05bb1-15cd-7e20-85e3-5a82b4333ffe`.
- **Result on both:** "Applicable interventions — Select up to four options. Every assumption is shown before you run them." followed by **"No applicable interventions are configured for this covenant."** There is nothing to select; the compare-against-doing-nothing step (F-01's climax, and all of F-04) cannot be performed at all.
- **Root cause, found exactly:**
  - `src/covenant_radar/db/seed/demo.py:210` hardcodes `covenant_class="financial"` for **every** Phase 7A demo covenant, regardless of type.
  - `src/covenant_radar/db/seed/data/interventions.json` (the only 3 seeded interventions) declares `applicable_covenant_classes` of `["conduct","liquidity"]`, `["leverage","coverage","liquidity"]`, and `["leverage","coverage","liquidity","net_worth"]` — **`"financial"` is not in any of these lists**, so `domain/interventions/applicability.py`'s matching logic (correctly) returns nothing for every demo covenant, every time.
- **Fix direction:** `db/seed/demo.py:210` should assign each demo covenant its real class (e.g. `"leverage"` for the `lev` covenant, `"liquidity"` for `liq`, etc. — matching the `kind` variable already used one line above at `demo.py:209` to build the covenant's name) instead of the single literal `"financial"`.

### 3. [BAD] The whole UI is a generic rounded-card SaaS dashboard, not the spec's ink-on-paper case file

- **Where:** Portfolio queue and Case file (the two most-protected screens per `plan.md §7.4`: *"the case file is the screen that carries the product... never traded against another screen's polish"*). Also present on the simulator and intake screens.
- **Evidence:** `base.html:20-27` loads, in order: `tokens.css`, `app.css`, `forecast.css`, `horizon.css`, `simulator.css`, `why.css`, **`modern.css`** (2,088 lines), `print.css`. `modern.css` defines its own parallel token set — `--radius-sm`, `--radius-md`, `--radius-pill`, `--shadow-sm`, `--shadow-md`, `--surface-raised`, `--surface-subtle`, `--signal-soft` — none of which are `spec.md §15.3`'s or `plan.md §7.2`'s named tokens (`--radius: 2px`, no shadow except the drawer, no gradient, no pill). It includes a `linear-gradient(...)` (line 142) and `border-radius: var(--radius-pill)` (line 243).
- **Visible symptoms:** a decorative gradient blob in the queue's hero panel; "Visible borrowers / Act now / Amber / Watch / Changed today / Portfolio exposure" rendered as bordered, drop-shadowed KPI tiles (`spec.md §15.2` forbid #1, by name: *"No KPI tiles or summary-card grids"*); the same KPI-tile pattern repeated verbatim in the case-file header's "four facts" (which are supposed to be plain ink-on-paper per `plan.md §7.4`); pill-shaped band chips and buttons instead of the mandated 2px radius; a blue/violet accent color (not in the ink/paper/headroom/watch/breach palette) used in the sidebar active state, all links, all buttons, and the horizon-control handle — `spec.md §15.2` forbid #2: *"No colour that is not risk."*
- **Confirmed via computed styles:** a KPI tile (`class="queue-summary-strip__metric queue-summary-strip__metric--total"`) computes to `border-radius: 12px`, `box-shadow: rgba(17,24,39,0.06) 0px 1px 2px 0px`, `background: rgb(255,255,255)` — all values foreign to `tokens.css`.
- **Fix direction:** this reads as a second, deliberately-built design system layered on top of the spec-compliant one, not an accident. The fix is almost certainly: remove `modern.css` and its `<link>` in `base.html:27`, then rebuild whatever markup depends on its classes (`queue-summary-strip__*`, the case-file header cards, band-chip pill radius, the horizon handle) against `tokens.css`'s actual variables, per `spec.md §15.3` and `plan.md §7.2`. This touches `T-020`/`T-021` (tokens/components) and every screen task that references `modern.css` classes (`T-073`, `T-075`–`T-078`, `T-079`, `T-097`).

### 4. [BAD] Every queue row shows a nonsensical "Urgency" value, e.g. "Urgency 62994%"

- **Where:** Portfolio queue table, Band column, every Act/Amber row.
- **Evidence (visible text, not a screen-reader-only label):** `<span class="band-chip band-chip--act">Act</span><span class="queue-row__urgency">Urgency 62994%</span>`. Other rows: 48707%, 47702%, 45998%, 45267%, 34696%, 32453% — all far past any sane percentage.
- **Root cause, found exactly:** `src/covenant_radar/web/view_models/queue.py:242` calls `urgency_display=_fraction_display(entry.urgency, label="Urgency")`. `_fraction_display` (same file, line 258) is documented as formatting *"a persisted **fraction**"* — it multiplies by 100 and appends `%` — and is correctly used for `confidence` one line above (a genuine 0–1 value, correctly shows "100%"). But `urgency` is not a 0–1 fraction: `domain/triage/urgency.py`'s `urgency()`/`compute_urgency()` combines `probability`, **`exposure`** (a rupee-crore magnitude, e.g. ₹636.30, ₹1,205.31) and `confidence`, so it is an unbounded, exposure-scaled score — running it through the same percentage formatter as confidence is the bug.
- **Fix direction:** stop calling `_fraction_display` on `entry.urgency` at `queue.py:242`; display the raw/rounded score (or a rank-based indicator) instead of a fabricated "%" suffix.

### 5. [BAD] Covenant "names" are the entire borrower legal name plus a raw class suffix

- **Where:** Case-file header ("Worst covenant" fact), Forecast trajectory panel title, Simulator, and the queue's "Worst covenant" column.
- **Evidence:** "Deshmukh Precision Materials and Components Kavya 00008 Private Limited **lev covenant**" / "…**cov covenant**" — the borrower's ~60-character legal name is repeated as the covenant's own label, 4–5 times on one screen (H1 title, "Borrower" fact, "Worst covenant" fact, forecast-panel title). Short codes (`D08COV`, `D08LEV`, `D08LIQ`) appear only as small print underneath.
- **Root cause, found exactly:** `src/covenant_radar/db/seed/demo.py:209` — `name=f"{borrower.legal_name} {kind.lower()} covenant"`.
- **Secondary effect:** the case-file header's 4-fact row (which spec requires to hold "exactly four facts and 40% whitespace") needs a **horizontal scrollbar even at 1920×1080** because the "Worst covenant" cell is forced this wide.
- **Fix direction:** give each seeded covenant a real name (e.g. "Leverage (TOL/TNW)", "Liquidity (Current ratio)") at `demo.py:209` instead of concatenating the borrower's legal name.

### 6. [BAD] Every page load throws a CSP violation blocking the app's own htmx

- **Where:** every screen; confirmed repeatedly on the portfolio queue's 60-second auto-refresh (3 separate occurrences over one polling cycle).
- **Console text (verbatim):** `Applying inline style violates the following Content Security Policy directive 'style-src 'self''. Either the 'unsafe-inline' keyword, a hash ('sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU='), or a nonce ('nonce-...') is required to enable inline execution... The action has been blocked. @ http://127.0.0.1:8000/static/vendor/htmx/htmx.min.js:0`
- **Root cause:** `src/covenant_radar/security/headers.py:40` sets `style-src 'self'` with no `unsafe-inline`, hash, or nonce. The vendored `htmx.min.js` (self-hosted, `T-021`) applies inline styles as part of its normal swap/settle mechanics, so the browser blocks it — the browser is even naming the exact hash (`sha256-47DEQpj8...`) that would allow it. This is same-origin, self-hosted script blocking itself; not a third-party CSP violation.
- **Fix direction:** add the named hash (or a per-request nonce, consistent with how script-src is presumably already handled) to `headers.py:40`'s `style-src` directive. Worth auditing whether any other htmx-driven interaction (loading skeletons, drawer open/close, filter submission) is silently degrading because of this — not confirmed either way in this pass.

### 7. [MINOR] Intake's empty-submit message doesn't match spec's required copy

- **Where:** `/intake` → "Enter covenant clause" → **Verify clause** with the Source text field empty.
- **Displayed:** "Provide the covenant text first."
- **Required (`spec.md §19`, row "Empty or whitespace clause input"):** *"Paste or select the covenant text first."*
- **Root cause:** `src/covenant_radar/web/routes/intake.py:383` — `raise ValidationError("Provide the covenant text first.", field="clause_text")`.
- **Caveat:** this pass submitted the form with *every* field blank (not source-text-only), so re-test with only Source text empty and the rest filled before assuming this is exactly spec §19's row and not a different generic-required-field message that happens to read similarly.
- **Fix direction:** change the literal string at `intake.py:383` to match spec's exact copy.

### 8. [MINOR] Borrower name column wraps catastrophically even at desktop width

- **Where:** Portfolio queue table, Borrower column, every row with a long legal name (i.e. most of them).
- **Evidence:** "Deshmukh Precision Materials and Components Kavya 00008 Private Limited" wraps to roughly **14 lines** inside a very narrow column, even at 1920×1080 — blowing the row far past the `--row-height: 32px` token and eating the "high density" the spec calls for in ledger tables.
- **Note for whoever fixes this:** `T-021`'s own unit test `test_long_name_wraps_not_truncates` only asserts that wrapping happens instead of truncation — it does not appear to check the column has a sane minimum width, so this could pass that test while still looking like this. Worth widening the column (or capping other columns) and adding a visual/width assertion, not just a wrap-vs-truncate one.

---

## Additional observations (not in the top 8, but worth folding into a fix pass)

- **Unlabeled field:** in `/intake`'s hand-entry form (`web/templates/screens/intake/index.html`), the "Facility reference" textbox has no accessible name in the a11y tree (plain `textbox`, no label), while the adjacent "Borrower reference" field is correctly labeled. Screen-reader users get an unlabeled control.
- **Inconsistent decimal precision:** money and percentages sometimes render with 4 decimal places (`₹636.3000`, `18.1500%`, `95.6000%` — the raw `numeric(18,4)` storage precision piped straight to the template) and sometimes with 2 (`₹13,471.43` in the portfolio-exposure tile). Pick one display convention.
- **No live-injection test completed:** the hostile-input check in this pass used the hand-entry path (empty submit + a `<script>`/instruction-injection string), which correctly HTML-escaped the script (no XSS fired) and correctly struck the unfilled fields — but hand entry does not call a model, so `spec.md §19`'s specific "Instruction-injection in any text sent to a model" row (which requires a document-upload → OCR → extraction path) was **not** exercised. Worth a follow-up pass with an actual PDF upload.

---

## Flows walked

- **F-01** (queue → case file → horizon control → why-panel → simulator → memo): walked successfully through the horizon control — confirmed keyboard-operable (`End` key jumps to day 90, values update, "Stored forecast day 90 loaded"), visible focus ring present, reads stored daily paths without a full page reload. **Blocked** at the simulator (finding #2) and memo (finding #1) steps.
- **F-02** (intake, partial): hand-entry path only, with empty input and hostile/script-injection input. Document upload / OCR path not tested.
- **Keyboard:** partial — horizon slider and one focus ring confirmed; no full page keyboard-only sweep.
- **Viewports:** 1920×1080 (projector) and 390×844 (mobile) checked — no horizontal page overflow at either. 1366×768 not checked.
- **spec §22 demo run:** could not be completed end-to-end — stops at the same two blocking findings.

## Not tested (ran out of scope after the two blocking failures)

Document/OCR upload path, case assignment + SLA, notifications, dark theme, submit-twice-fast, reload-mid-form, browser-back, offline/model-down state, full screen-reader pass, 1366×768 viewport.

## Cleanup performed

The database (`var/covenant-radar.db`) and `var/documents` were backed up before this pass began, the server was stopped, both were restored from that backup after testing, and the server was restarted — row counts (`borrower=5000`, `covenant=108`, `document=0`, `memo=0`, identical `job_run` history) were verified identical to the pre-test baseline. No product code was changed. Stray screenshots taken during this pass were deleted from the repo root.
