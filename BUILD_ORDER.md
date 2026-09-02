# BUILD_ORDER.md — Covenant Radar · the 24-hour execution order

**Supersedes the sequencing in `tasks.md §2.2`–`§2.3` for the hackathon window only.** Task *content* is unchanged: every block in `tasks.md` is worked exactly as written. This document changes only **which block is worked next**, and which are out of scope.

Derived from the full dependency graph of all 173 blocks. Effort is the plan's own `Days:` value converted at the measured rate of **34 ideal-days in 7 hours** (4.86/hour), de-rated for milestone density (62% on the scoring mathematics and the AI layer, 80–85% elsewhere).

---

## The rule this order guarantees

**No task in this list is blocked.** Every `Depends on` entry of every task below is satisfied either by `T-001`–`T-023` (already implemented) or by a task appearing *earlier in this list*. This was verified mechanically against every dependency edge in `tasks.md`; the check reports zero violations. Work strictly top to bottom and you can never arrive at a block whose prerequisite does not exist.

---

## Removed from scope

| Task | Title | Reason |
|---|---|---|
| `T-140` | Translation catalogues, extraction and the build check | Removed with the Hindi feature — the catalogue machinery exists only to serve it. |
| `T-141` | Hindi translation and locale formatting | Removed by decision. |

**Consequences, checked against the whole backlog.** Only `T-141` and `T-159` referenced these blocks. `T-141` is itself removed; `T-159` (end-to-end suite across themes and languages) is already out of scope, and when it returns it narrows to themes only. **No task in this build order depends on either removed block.**

The dormant `src/covenant_radar/i18n/` scaffold from `T-022` is left in place and untouched — it is inert, and removing it would alter an already-implemented code path. English strings render directly.

---

## Version safety without a version-control system

`tasks.md §3` steps 2, 6 and 7 assume branching and merging. Replace them with a snapshot protocol:

```
Before starting task T-0NN:
  robocopy src  var\snapshots\T-0NN\src  /MIR /NFL /NDL /NJH /NJS
  robocopy tests var\snapshots\T-0NN\tests /MIR /NFL /NDL /NJH /NJS

To undo T-0NN:
  robocopy var\snapshots\T-0NN\src  src  /MIR /NFL /NDL /NJH /NJS
  robocopy var\snapshots\T-0NN\tests tests /MIR /NFL /NDL /NJH /NJS
```

`var/` is already ignored and is not packaged. Take the snapshot **before** the task, not after — the snapshot is the state you want back if the task goes wrong. Record each completed task as a row in `MERGE_LOG.md` (task id, snapshot path, planned hours, actual hours) so the ledger stays meaningful.

---


## Phase 1 · Covenant core computes

`11 tasks` · `18.5 ideal-days` · `~4.5h this phase` · **cumulative 4.5h**

**Gate — demonstrate before moving on:** A signed covenant tests correctly against real statements, with exceptions, waivers, cure periods and not-computable all exact.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 1 | `T-024` | Statement chart of accounts and the normalisation model | 1.5 | 0.4h | `T-010`* |
| 2 | `T-027` | Ratio library part 1 — leverage, coverage and liquidity | 1.5 | 0.7h | `T-024` |
| 3 | `T-028` | Ratio library part 2 — conduct, working capital and covenant conditions | 1.5 | 1.1h | `T-027` |
| 4 | `T-030` | Not-computable and missing-line behaviour across the library | 0.5 | 1.2h | `T-028` |
| 5 | `T-031` | Covenant registry: model, versioning, immutability enforcement | 2.0 | 1.7h | `T-010`* |
| 6 | `T-032` | Exceptions, waivers, cure and grace periods | 1.5 | 2.1h | `T-031` |
| 7 | `T-033` | Registry service, maker-checker path and API | 1.5 | 2.4h | `T-032`, `T-018`* |
| 8 | `T-034` | Covenant engine: evaluation, headroom, verdicts, boundaries | 2.5 | 3.0h | `T-031`, `T-030` |
| 9 | `T-037` | Stage-2 trace rows and engine explainability data | 1.5 | 3.4h | `T-034` |
| 10 | `T-040` | Reference portfolio: borrowers, facilities, financials | 2.5 | 4.0h | `T-011`* |
| 11 | `T-041` | Reference portfolio: cohorts, signals and labelled outcomes | 2.0 | 4.5h | `T-040` |

## Phase 2 · AI spine, documents and grounded intake

`13 tasks` · `20.5 ideal-days` · `~6.8h this phase` · **cumulative 11.3h**

**Gate — demonstrate before moving on:** A sanction letter becomes proposed covenant fields; one clause is struck by a named failing check; the rest confirm and test on the spot. Runs offline against cassettes.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 12 | `T-088` | LLM provider protocol and the four adapters | 2.0 | 5.1h | `T-004`* |
| 13 | `T-089` | The single call site: retries, timeouts, ceilings, budget, logging | 1.5 | 5.6h | `T-088` |
| 14 | `T-090` | Outbound masking whitelist that fails closed | 1.5 | 6.1h | `T-089` |
| 15 | `T-091` | Recorded-response adapter and cassette management | 0.5 | 6.3h | `T-088` |
| 16 | `T-092` | Prompt files, version binding and the build check | 1.0 | 6.6h | `T-089` |
| 17 | `T-084` | Document model, upload, virus scan, encrypted store | 2.0 | 7.3h | `T-017`*, `T-019`* |
| 18 | `T-085` | Native PDF text and span extraction | 2.0 | 8.0h | `T-084` |
| 19 | `T-086` | OCR pipeline, page confidence, human-review routing | 2.0 | 8.6h | `T-085` |
| 20 | `T-087` | Document classification and the span-highlighting viewer | 1.5 | 9.1h | `T-086`, `T-021`* |
| 21 | `T-093` | Clause candidate detection over documents and text | 1.5 | 9.6h | `T-085` |
| 22 | `T-094` | Stage-1 proposal, parsing and normalisation | 1.5 | 10.1h | `T-093`, `T-092` |
| 23 | `T-095` | The six code verifications, failing closed | 2.0 | 10.8h | `T-094`, `T-034` |
| 24 | `T-096` | Intake service, confirm refusal and the approval flow | 1.5 | 11.3h | `T-095`, `T-033` |

## Phase 3 · Evidence ledger, 30/60/90 forecast and drivers

`17 tasks` · `24.5 ideal-days` · `~8.1h this phase` · **cumulative 19.4h**

**Gate — demonstrate before moving on:** A transient blip decays while a sustained pattern escalates to a dated crossing with named drivers and a visible confidence.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 25 | `T-042` | Signal event model and the ingestion framework | 2.0 | 12.0h | `T-010`* |
| 26 | `T-046` | Evidence item model and derivation from events | 1.5 | 12.5h | `T-042` |
| 27 | `T-047` | Persistence scoring | 1.5 | 12.9h | `T-046`, `T-012`* |
| 28 | `T-048` | Materiality scoring | 1.5 | 13.4h | `T-047`, `T-034` |
| 29 | `T-049` | Decay and visibility | 1.0 | 13.8h | `T-047` |
| 30 | `T-050` | Supersession and revision | 1.5 | 14.3h | `T-049` |
| 31 | `T-051` | Stage-3 trace rows and ledger explainability | 1.0 | 14.6h | `T-050` |
| 32 | `T-052` | Trend projection and the daily path | 2.0 | 15.3h | `T-034`, `T-050` |
| 33 | `T-053` | Threshold crossing and dating | 1.5 | 15.8h | `T-052` |
| 34 | `T-054` | Probability mapping, clamping and term capture | 1.5 | 16.3h | `T-053` |
| 35 | `T-055` | Confidence model from completeness, support and staleness | 1.5 | 16.8h | `T-054` |
| 36 | `T-056` | Forecast persistence, runs, versioning and staleness marking | 1.5 | 17.3h | `T-055` |
| 37 | `T-057` | Driver attribution and normalisation | 1.5 | 17.8h | `T-056` |
| 38 | `T-058` | Attribution links to evidence and stage-4 trace | 1.0 | 18.1h | `T-057` |
| 39 | `T-059` | Urgency, banding and the deterministic tie-break | 1.5 | 18.6h | `T-056` |
| 40 | `T-060` | What-changed computation between runs | 1.0 | 18.9h | `T-059` |
| 41 | `T-061` | Queue query, filtering and the saved-view model | 1.5 | 19.4h | `T-060`, `T-016`* |

## Phase 4 · Audit chain and screens — WORKING PRODUCT

`10 tasks` · `18.0 ideal-days` · `~4.8h this phase` · **cumulative 24.2h**

**Gate — demonstrate before moving on:** Queue to case file to 30/60/90 with drivers, every figure resolving through the why-panel, plus the side-by-side intake screen. Ship from here.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 42 | `T-066` | Audit store, hash chain and append-only enforcement | 2.0 | 19.9h | `T-010`* |
| 43 | `T-067` | Audit emission across every service, with the coverage test | 1.5 | 20.3h | `T-066` |
| 44 | `T-068` | Warning reconstruction assembly | 1.5 | 20.7h | `T-067`, `T-058` |
| 45 | `T-070` | Trace model, the unified stage record and its reader | 1.5 | 21.1h | `T-066` |
| 46 | `T-071` | Why-panel rendering for code, model and statistical stages | 2.0 | 21.6h | `T-070`, `T-021`* |
| 47 | `T-073` | Portfolio queue screen | 2.0 | 22.1h | `T-061`, `T-021`* |
| 48 | `T-075` | Case file: layout, header facts, covenant strip | 2.0 | 22.6h | `T-073` |
| 49 | `T-076` | Forecast panel and inline SVG trajectories | 1.5 | 23.0h | `T-075`, `T-056` |
| 50 | `T-077` | Horizon control: interaction, keyboard, reduced motion, stops fallback | 2.0 | 23.5h | `T-076` |
| 51 | `T-097` | Intake screen: side-by-side, inline verdicts, hand entry | 2.0 | 24.2h | `T-096`, `T-087` |

## Phase 5 · Intervention simulation and the memo

`7 tasks` · `10.5 ideal-days` · `~3.5h this phase` · **cumulative 27.7h**

**Gate — demonstrate before moving on:** Three interventions compared against doing nothing, and a grounded memo whose every figure resolves to a record.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 52 | `T-062` | Intervention effect models and applicability rules | 1.5 | 24.7h | `T-052` |
| 53 | `T-063` | Counterfactual simulation and multi-option comparison | 2.0 | 25.4h | `T-062` |
| 54 | `T-064` | Simulation persistence and assumption capture | 1.0 | 25.7h | `T-063` |
| 55 | `T-098` | Action catalogue: model, management and applicability | 1.5 | 26.2h | `T-062` |
| 56 | `T-099` | Memo slot assembly from records only | 1.5 | 26.7h | `T-058`, `T-064` |
| 57 | `T-100` | Stage-7 prompt, drafting and the four shape checks | 2.0 | 27.4h | `T-099`, `T-092` |
| 58 | `T-101` | Memo refusal, retry and persistence rules | 1.0 | 27.7h | `T-100` |

## Phase 6 · Drawdown — only if time remains

`12 tasks` · `19.0 ideal-days` · `~5.4h this phase` · **cumulative 33.1h**

**Gate — demonstrate before moving on:** Take strictly in this order. Each entry is a clean re-queue of an untouched block.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 59 | `T-120` | Job model, scheduler, run ledger, restart resumption | 2.0 | 28.2h | `T-010`* |
| 60 | `T-121` | Nightly pipeline composition and idempotent re-run | 2.0 | 28.7h | `T-120`, `T-060` |
| 61 | `T-103` | Evaluation example schema and the authored set | 2.0 | 29.4h | `T-041` |
| 62 | `T-104` | Evaluation harness: the product arm | 2.0 | 30.1h | `T-103`, `T-091` |
| 63 | `T-105` | Evaluation harness: the baseline arm and the scoreboard | 1.5 | 30.5h | `T-104` |
| 64 | `T-102` | Memo PDF and DOCX export with integrity hash | 1.5 | 31.0h | `T-101` |
| 65 | `T-069` | Evidence bundle export, manifest and verification | 1.5 | 31.4h | `T-068` |
| 66 | `T-080` | Audit search and reconstruction screens | 1.5 | 31.8h | `T-069` |
| 67 | `T-079` | Simulator screen and comparison view | 1.5 | 32.2h | `T-064`, `T-076` |
| 68 | `T-111` | Override capture and view revision | 1.5 | 32.6h | `T-067`, `T-071` |
| 69 | `T-074` | Queue filters, saved views and bulk selection | 1.0 | 32.8h | `T-073` |
| 70 | `T-072` | Why-panel API and the no-JavaScript full page | 1.0 | 33.1h | `T-071` |

`*` = already implemented in `T-001`–`T-023`.

---

## Budget

| Milestone in this order | Tasks | Ideal days | At measured pace | De-rated |
|---|--:|--:|--:|--:|
| Phase 1–4 — **working product floor** | 51 | 81.5 | 16.8h | 24.2h |
| Phase 1–5 — recommended target | 58 | 92.0 | 18.9h | 27.7h |
| Phase 1–6 — full scoped set | 70 | 111.0 | 22.9h | 33.1h |

Plan against the de-rated column. Phase 1–4 is the point at which the product is complete and demonstrable; Phases 5 and 6 add depth, not viability.

---

## Working rhythm

1. Snapshot `src/` and `tests/` to `var/snapshots/T-0NN/`.
2. Read the block in `tasks.md` and only the paths its `Read first` names, plus the `plan.md §6` contract rows it lists.
3. Build inside `Files owned`. Write the named tests as you go.
4. Run `python -m radarctl gate --fast`, then each `Run` command with its stated expected result.
5. Record the row in `MERGE_LOG.md`.
6. At each phase boundary run the **full** `python -m radarctl gate` and demonstrate the phase gate above.

A task that exceeds its estimate by more than half is stopped and split, per `tasks.md §2.2`. Re-forecast the remaining phases from the observed ratio at the end of Phase 2 and Phase 4.

---

## Out of scope for this window

78 blocks remain in `tasks.md` untouched and unmodified — connectors and feeds, the public REST API, notifications and case workflow, regulatory reporting, observability, installers, compliance and release engineering. Nothing was deleted, so reintroducing any of them is a re-queue rather than a rebuild.

**If time remains after Phase 6, restore in this order:** `T-025` statement import (0.6h) · `T-036` SMA banding (0.3h) · `T-065` threshold calibration (1.0h) · `T-135`+`T-136` REST API and OpenAPI (1.0h).

---

## Phase 7 · The finishing pass — wired, populated, presentable

**Added 2026-08-31, after running the app on this laptop.** Every `T-0NN` block above this line was built and individually tested — the ledger and gate evidence back that up. But building a component and *wiring it into the running application* are different acts, and this pass exists because the second one was skipped in three places that matter more than any single feature: nothing that ships past this point counts if the judges are looking at an empty queue behind an unstyled login form.

**Method.** Everything below was verified by actually starting the app (`radarctl serve`, the existing `var/covenant-radar.db`) and driving it — not by reading code and assuming. Each task states the exact evidence found. New task IDs use an `H-` prefix (for "hackathon finishing pass") so they're never confused with the frozen `T-0NN` catalogue in `tasks.md` — these are not in `tasks.md` and don't need to be; `BUILD_ORDER.md` already governs sequencing, not `tasks.md` content, and this phase is pure sequencing-and-wiring work over code that (mostly) already exists. Snapshot protocol from the top of this document still applies before each task.

**Explicitly out of scope, per your instruction — do not work these even if time remains:** Windows/Linux installers and `deploy/` packaging, production SSO/SAML wiring, PostgreSQL/production deployment, notifications/webhooks/SMTP, regulatory reporting, observability/tracing/metrics export, translation catalogues (already removed as `T-140`/`T-141`). None of it is visible in a one-laptop, one-browser, judged demo.

### 7A · Wire the composition root — nothing above this line can be seen without it

**Gate — demonstrate before moving to 7B:** `radarctl job run nightly.pipeline` completes all six steps against seeded data and `triage_entry`/`forecast`/`case` rows exist; a document uploads and is retrievable; an intake proposal round-trips through a recorded AI response without a `provider_unavailable` state.

| # | Task | Title | Est. | Depends on | Evidence found on this laptop |
|--:|---|---|--:|---|---|
| 1 | `H-01` | Register the nightly pipeline against a real, running `NightlyPipelineService` | 1.0h | `T-120`, `T-121` (built) | `register_nightly_pipeline` and `pipeline_job` (`scheduler/pipeline.py`) are never called anywhere in `src/` outside their own tests. `radarctl job run nightly.pipeline` fails today with `Job trigger refused: no job named 'nightly.pipeline' is registered.` Construct `NightlyPipelineService` in `cli.py`'s job-command path (and in `web/application.py`'s startup so `serve` can trigger it too) with a real `session_factory`, a `ThresholdSnapshotProvider` backed by the calibration this DB already has schema for, the configured `Weights`, and a `system_actor_id`; register it against `default_registry()`. **Done when:** `radarctl job run nightly.pipeline` exits 0 and `nightly.ingest/test/score/rank/update_cases/dispatch` each report `succeeded` in `job_run`. |
| 2 | `H-02` | Wire the real document store into `web/application.py` | 0.5h | `T-084` (built) | `web/application.py:64-79` hardcodes `DisabledDocumentStore()` — every `put`/`get`/`delete`/`stream` unconditionally raises `ExternalServiceError`. It does not branch on `settings.documents.store` at all; setting `COVENANT_RADAR_DOCUMENTS__STORE=local` in `.env` today changes nothing. `documents/store.py::FileSystemDocumentStore` already exists and is tested (`T-084`'s 9 required tests). Branch on `settings.documents.store` (`none` → keep `DisabledDocumentStore`; `local` → `FileSystemDocumentStore(settings.documents.local_path)`) in both places `DisabledDocumentStore()` is constructed. **Done when:** uploading a PDF through `/intake` or a borrower's document panel persists it and `GET`s it back byte-identical; `.env` sets `COVENANT_RADAR_DOCUMENTS__STORE=local` and `COVENANT_RADAR_DOCUMENTS__LOCAL_PATH=var/documents`. |
| 3 | `H-03` | Wire an LLM provider into the intake composition root | 1.0h | `T-088`–`T-096` (built), `H-05` | No `AiSettings`, `create_llm_provider`, or any `ai.*` import appears in `web/application.py`. The intake route (`web/routes/intake.py`) already has clean `ProviderError` → `provider_unavailable` handling — it just never receives a live provider, so every intake run today takes the hand-entry path regardless of `.env`. Build the provider from `covenant_radar.ai` per `settings.ai.provider` in `web/application.py` and pass it through to whatever constructs the stage-1 candidate-detection/proposal call (`T-093`/`T-094`'s service, upstream of `IntakeService`). Point `.env`'s `COVENANT_RADAR_AI__PROVIDER` at `recorded` for this laptop — no key, no network, fully offline, matching the cassette machinery `T-091` already built. **Done when:** an intake run against a demo document returns real proposed fields (not the provider-down hand-entry screen) sourced from a cassette. |
| 4 | `H-04` | Demo covenants, financials and calibrated thresholds for a curated borrower slice | 1.5h | `T-024`, `T-031`, `T-065` (built), `H-01` | `seed --reference-portfolio` loads 5,000 borrowers and 12,000 facilities but **zero** rows in `covenant`, `covenant_version`, `financial_period`, `statement_line_value`, or `threshold_snapshot` — confirmed by direct row counts on `var/covenant-radar.db`. The nightly pipeline has nothing to test even once `H-01` is wired. Extend `radarctl seed` with a `--demo-covenants` option that, for ~30–50 named borrowers drawn from the reference portfolio, registers signed leverage/coverage/liquidity covenants (`T-031`'s registry service), imports several periods of financial statements, and loads `T-065`'s calibrated threshold snapshot. Deliberately curate outcomes so the queue tells a story: several borrowers already in breach, several crossing at each of 30/60/90 days with a named dominant driver, several comfortably in headroom. **Done when:** `radarctl seed --demo-covenants` is idempotent, and after `H-01`'s pipeline run the queue has rows spanning all three risk bands with real dated crossings. |
| 5 | `H-05` | Author demo cassettes for the recorded AI provider | 1.0h | `T-091`, `T-092` (built), `H-04` | `T-091`'s cassette adapter and `T-092`'s prompt/version machinery exist and are tested, but no cassette content covering the demo documents exists yet — `H-03` has nothing to play back without this. Using `radarctl cassette record` (or hand-authoring per the recorded-format `T-091` defines) against 2–3 realistic sanction-letter-style source documents matched to `H-04`'s demo borrowers, record the stage-1 proposal response and one stage-7 memo-drafting response, including one deliberately failing clause so the "one clause struck by a named check" moment in the Phase 2 gate is demonstrable live. **Done when:** `radarctl cassette replay` returns each recorded response deterministically offline. |
| 6 | `H-06` | One-command demo bootstrap | 0.5h | `H-01`–`H-05` | There is currently no single path from a fresh checkout to a populated, demoable app — every finding above had to be discovered and fixed by hand. Add `scripts/demo_up.ps1`: `pip install -e .` → `radarctl migrate upgrade` → `radarctl seed --reference-portfolio` → `radarctl seed --demo-covenants` → `radarctl job run nightly.pipeline` → ensure the presenter's user exists (reuse `create_user.py`'s logic, don't hand-roll a second copy) → `radarctl serve`. **Done when:** a teammate runs one script on a clean clone of this repo and reaches a fully populated, signed-in app with no manual steps. |

### 7B · Fix what's visibly broken — the first eight seconds

**Gate — demonstrate before moving to 7C:** every full-page screen in the app, signed out and signed in, renders with the token-driven typography and palette from `tokens.css` — none render as bare browser-default HTML.

| # | Task | Title | Est. | Depends on | Evidence found on this laptop |
|--:|---|---|--:|---|---|
| 7 | `H-07` | Make the four auth screens extend `base.html` | 1.0h | none | Verified directly: `GET /sign-in` returns a bare `<!doctype html>` document with **no `<link>` tag at all** — no `tokens.css`, no `app.css`, no fonts, default Times New Roman heading, unstyled button. Confirmed the same by grep: `screens/auth/sign_in.html`, `change_password.html`, `mfa_enrol.html`, `mfa_verify.html` are the only full-page (non-fragment) templates in the whole tree that don't `extend "base.html"` — every other screen already does. This is the literal front door of the product. Rewrite all four to extend `base.html`, using the existing `components.html` macros (`field`, `button`) rather than raw `<input>`/`<button>` markup, and keep the pre-authentication chrome minimal (no portfolio nav links a signed-out user can't use) but on-brand: paper background, serif wordmark, token spacing. **Done when:** `GET /sign-in` includes `tokens.css`/`app.css` and renders with the product's actual palette and type, verified visually. |
| 8 | `H-08` | Full-screen visual QA pass against real, populated data | 1.5h | `H-01`–`H-07` | Every screen was built and unit/integration-tested in isolation against synthetic fixtures, but no one has looked at the queue, a case file, the forecast panel, the why-panel, the intake screen and the simulator rendered together, signed in, against `H-04`'s populated data, in both themes. Walk every screen in `BUILD_ORDER.md`'s own Phase 1–5 gates in order, screenshot each (light and dark), and fix concrete defects found — not hypothetical ones. Pay particular attention to: the queue table at realistic row counts (does it stay legible, does horizontal scroll ever trigger), the trajectory SVG (`svg/trajectory.py` refuses to render without a persisted path — confirm it actually has one now that `H-01`/`H-04` populated `forecast_path`), and dark-mode contrast on the risk chips. **Done when:** a screenshot of every screen in both themes exists in `var/qa-screenshots/` and every defect found is either fixed or explicitly logged as accepted. |

### 7C · The stunning pass — design investment where judges are actually looking

**Do this only once 7A and 7B are green — a beautifully designed empty screen is still an empty screen.** The existing token system (`tokens.css`, `docs/adr/0003-typography-and-theming.md`) is a genuinely considered, editorial/ledger aesthetic — a warm paper-and-ink palette, three deliberate type roles, risk colors that keep their meaning in dark mode — not generic Bootstrap-blue AI slop. The work here is *extending that same discipline into the moments that sell the product*, not replacing it.

| # | Task | Title | Est. | Depends on | Notes |
|--:|---|---|--:|---|---|
| 9 | `H-09` | A five-second portfolio summary strip above the queue | 1.0h | `H-08` | Right now `queue/index.html` opens straight into a dense filter form and ledger table — correct for a working session, but it gives a judge nothing to read in the first five seconds. Add a compact strip above the filters (reusing `band_chip`/`components.html`, no new visual language): counts by band, today's what-changed count, portfolio exposure total. Token-only styling, no new colors. |
| 10 | `H-10` | Make the forecast trajectory the visual centerpiece of the case file | 1.0h | `H-08` | The inline-SVG trajectory (`T-076`/`T-077`, `svg/trajectory.py`) is well-engineered — it refuses to render without real ledger data, which `H-01`/`H-04` now provide. Give it more visual weight in `borrower/_forecast.html`: larger viewport, the crossing-day driver named directly on the chart instead of only in the caption below it. No new chart library — extend the existing accessible SVG renderer. |
| 11 | `H-11` | Row-level mini-trajectories in the queue | 1.0h | `H-10` | Once the full-size trajectory is solid, reuse `svg/trajectory.py` at a small size for a sparkline-style preview per queue row, so risk direction is visible without a click. Purely additive to the existing ledger table markup. |
| 12 | `H-12` | Rehearse the why-panel as a deliberate credibility beat | 0.5h | `H-08` | The explainability drawer (`T-070`–`T-072`) is the product's actual differentiator — "every figure resolves to a record" — but it's easy to demo as just another accordion. No code change: confirm each of the four trace-stage types renders legibly with real data from `H-04`, and write the two-sentence talking point that goes with opening it live on a breach figure. |

### 7D · Rehearsal

| # | Task | Title | Est. | Depends on |
|--:|---|---|--:|---|
| 13 | `H-13` | A scripted, timed demo runbook | 0.5h | `H-01`–`H-12` | Write `docs/demo-runbook.md`: the exact click path (sign in → queue → a chosen breach-bound case → forecast panel → why-panel on one figure → simulator comparing two interventions → intake on a demo document → the drafted memo), the exact borrower names from `H-04` to use, and a "if X breaks, say Y and move to Z" fallback for each step. Run it start to finish on this laptop at least twice before presenting. |

### Budget for this phase

| Group | Tasks | Est. hours |
|---|--:|--:|
| 7A — wire the composition root (must-do, blocks everything below) | 6 | ~5.5h |
| 7B — fix what's visibly broken (must-do) | 2 | ~2.5h |
| 7C — the stunning pass (do as time allows, in order) | 4 | ~3.5h |
| 7D — rehearsal (must-do, last) | 1 | ~0.5h |

If time is short, 7A + 7B + `H-13` (≈8.5h) is the floor: a real, populated, on-brand, rehearsed product. 7C is depth, not viability — same principle `BUILD_ORDER.md` already applies to Phase 5/6 above.

---

## Phase 8 · Full spec completion — every remaining feature `spec.md` describes

**Added 2026-09-01, at your direction; revised the same day against `tasks.md §7`'s own requirement-traceability table.** `tasks.md` has 173 blocks. `T-001`–`T-023` (the `M-0` foundation) and the 70 blocks Phases 1–6 above sequenced are now marked `[x]` directly in `tasks.md` — both in its own `§2.3` order table and on each task's own `### [x] \`T-0NN\`` heading — along with `T-065` and `T-135`, which the ledger in `MERGE_LOG.md` shows were pulled forward and finished ahead of their formal queue position. That's **95 of 173 blocks done.** The remaining 78 are exactly the set `tasks.md §2.4` calls "out of scope for this window" — their briefs were never deleted, only deferred.

**Revision note.** The first pass of this phase queued 30 tasks by title and category, the same way `tasks.md §2.4` groups them. Checking that against `tasks.md §7 Table 1` — which maps every numbered requirement `R-01`…`R-36` to the tasks that build it — found four requirements only partially covered by that first pass: **`R-26` administration console** (missing `T-115`), **`R-27` notifications** (missing `T-117`, `T-118`), **`R-28` batch orchestration** (missing `T-122`), and **`R-31` regulatory reporting** (missing `T-134`, which was blocked on `T-117`). It also found `N-01`'s evaluation harness one task short of complete (`T-106`), and three failure cases in `tasks.md §7 Table 2` — "Database unavailable," "Crash or restart mid-batch," "Clock skew detected" — whose mitigating task, `T-149`, was excluded along with production observability generally, even though a demo that crashes mid-pipeline-run is exactly the failure this hackathon can least afford. `T-149` itself needs `T-143` (health/readiness endpoints), which needs `T-142` (log redaction/rotation) — both cheap, self-contained, and worth having regardless: nobody wants a stray Argon2 hash or session secret sitting in a log file a judge could see over your shoulder. Those nine tasks are folded into the phase below; **`T-108` (drift monitoring against live traffic) and `T-144`/`T-145` (tracing and alerting against a monitoring stack this laptop doesn't run) are the only pieces of those requirements that stay out** — genuinely inert without infrastructure no demo has.

This phase now re-queues **39 of the 78** deferred blocks — every one that is a genuine product feature `spec.md` describes and that a single laptop can actually demonstrate. **Every `R-01`–`R-36` functional requirement in `tasks.md §7 Table 1` is now fully covered by either an already-done task or a task in this phase — zero partial requirements remain.** The other **39 stay deferred** (37 blocks plus `T-140`/`T-141`, already struck): installers, packaging, live-traffic observability and drift monitoring, performance/capacity engineering, backup/restore, compliance and retention, penetration testing, release engineering, and documentation. None of that is a feature a user or a judge interacts with. The full list is in **"Still deferred"** below.

**Sequencing.** Every `Depends on` below is satisfied either by an already-`[x]`-marked task or by a task earlier in this same phase — checked by hand against each block's own `Depends on:` field in `tasks.md §5`. Work Phase 7 first: it wires and populates what's already built, and every screen this phase adds (case screens, admin console, governance) needs that same working pipeline to show real data against. Effort figures are each block's own `Days:` value from `tasks.md`, converted at the same measured rate the rest of this document uses (34 ideal-days ≈ 7h, de-rated 80%); `Cum` continues from Phase 6's 33.1h.

### 8A · Covenant admin depth

**Gate:** a statement imports from a spreadsheet with row-level validation and provenance; a custom covenant formula evaluates; the testing calendar schedules a retest on arrival; SMA bands and covenant certificates track conduct end to end.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 1 | `T-025` | Statement import: CSV, XLSX, JSON and API, with mapping and validation | 2.5 | 33.5h | `T-024`✓ |
| 2 | `T-026` | Provenance, restatement and quarantine resolution | 1.5 | 33.8h | `T-025` |
| 3 | `T-029` | Custom formula parser, validator and restricted evaluator | 1.5 | 34.0h | `T-027`✓ |
| 4 | `T-035` | Testing calendar, scheduling and on-arrival retest | 1.5 | 34.3h | `T-034`✓ |
| 5 | `T-036` | SMA banding from account conduct | 1.0 | 34.4h | `T-034`✓ |
| 6 | `T-038` | Certificate model and request generation from the calendar | 1.5 | 34.7h | `T-035` |
| 7 | `T-039` | Certificate receipt, linkage, rejection and overdue evidence | 1.0 | 34.8h | `T-038` |

### 8B · Signal plumbing

**Gate:** the nightly pipeline's `nightly.ingest` step (dead code today — see Phase 7's `H-01`) has a real, robust source to ingest from, with duplicate/late-arrival handling and a quarantine report instead of silently dropping malformed rows.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 8 | `T-043` | Signal source adapters and the connector hand-off point | 1.0 | 35.0h | `T-042`✓ |
| 9 | `T-044` | Idempotence, late arrival and watermarking | 1.5 | 35.2h | `T-042`✓ |
| 10 | `T-045` | Ingestion reporting and quarantine for signals | 0.5 | 35.3h | `T-044` |

*Coordinate with Phase 7's `H-01`/`H-04`: those tasks wire a minimal signal source to get the pipeline running for the demo before this section exists. Once `T-043` is built, point `H-01`'s wiring at the real adapter instead of the throwaway one.*

### 8C · Interface polish

**Gate:** every screen the product ships has an evidence margin with linked documents and case actions, a governance screen a risk-committee member could read cold, a complete dark theme (not just Phase 7's `H-08` spot-check), and a passing automated accessibility audit.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 11 | `T-078` | Evidence margin, document strip and case actions | 1.5 | 35.6h | `T-075`✓, `T-051`✓ |
| 12 | `T-081` | Governance screens: thresholds, model registry, scoreboard | 1.5 | 35.8h | `T-080`✓, `T-012`✓ |
| 13 | `T-082` | Dark theme completion and print styles | 1.5 | 36.1h | `T-078` |
| 14 | `T-083` | Automated accessibility audit and remediation | 2.0 | 36.4h | `T-082` |

### 8D · Case workflow and admin console

**Gate:** a warning becomes a case with a real SLA clock, an analyst can comment and record actions taken, a closed case can be dispositioned as true/false positive and exported for the evaluation harness, and an administrator can manage users, roles and thresholds without touching the database.

**`R-26` administration console and `R-27` notifications now fully covered** — this sub-phase was the source of both partial requirements the revision note above found.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 15 | `T-109` | Case model, SLA derivation and lifecycle | 2.0 | 36.7h | `T-059`✓ |
| 16 | `T-110` | Case screens, comments, actions taken | 1.5 | 37.0h | `T-109`, `T-075`✓ |
| 17 | `T-112` | Disposition, feedback and labelled-dataset export | 1.5 | 37.2h | `T-111`✓ |
| 18 | `T-113` | Admin console: users, roles, scoping, sessions | 2.0 | 37.6h | `T-016`✓, `T-022`✓ |
| 19 | `T-114` | Admin console: thresholds with approval, action catalogue | 1.5 | 37.8h | `T-113`, `T-098`✓ |
| 20 | `T-115` | Admin console: jobs, health, retention configuration | 1.0 | 38.0h | `T-113` |
| 21 | `T-116` | Notification model, templates, preferences, quiet hours | 1.5 | 38.2h | `T-010`✓ |
| 22 | `T-117` | Email digests and bundling | 1.5 | 38.5h | `T-116` |
| 23 | `T-118` | Webhook delivery: signing, retry, dead letter | 1.5 | 38.7h | `T-116` |
| 24 | `T-119` | In-app notification centre | 1.0 | 38.9h | `T-116`, `T-022`✓ |

*Demo `T-117`/`T-118` against a local test target — a debug SMTP server (e.g. Python's `smtpd`/`aiosmtpd`) and a local webhook receiver — not a production mail host. The requirement is the signing/retry/dead-letter/bundling logic working correctly, which a local target proves as well as a real one; that's what `tasks.md`'s own tests for these blocks already exercise.*

### 8E · Model governance

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 25 | `T-107` | Model registry, model cards and the approval path | 1.5 | 39.2h | `T-089`✓ |

### 8F · Regulatory reporting

**Gate:** a CRILC-shaped weekly default report, an EWS/RFA pack, and a Board MIS report all generate from real case data — domain-authentic outputs a credit-risk judge will recognise.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 26 | `T-132` | CRILC export and the weekly default report | 2.0 | 39.5h | `T-036` (8A), `T-056`✓ |
| 27 | `T-133` | EWS/RFA pack assembly | 1.5 | 39.8h | `T-068`✓ |
| 28 | `T-134` | Board MIS and scheduled report delivery | 1.0 | 39.9h | `T-132`, `T-117` (8D) |

### 8G · Public API, search and bulk operations

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 29 | `T-136` | API keys, scoping, rate limits, OpenAPI and contract tests | 2.0 | 40.2h | `T-135`✓ |
| 30 | `T-137` | Search across entities with scope enforcement | 1.5 | 40.5h | `T-016`✓ |
| 31 | `T-138` | Saved views, recent items and sharing | 1.0 | 40.6h | `T-137`, `T-074`✓ |
| 32 | `T-139` | Bulk operations and asynchronous export | 1.5 | 40.9h | `T-074`✓, `T-120`✓ |

### 8H · Synthetic feeds and integrity checks

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 33 | `T-128` | Feed adapter protocol and the synthetic generator | 1.5 | 41.1h | `T-042`✓ |
| 34 | `T-150` | Data-integrity checks: audit chain and referential | 1.0 | 41.3h | `T-066`✓ |

### 8I · Evaluation harness completion and demo-day resilience

**Gate:** `radarctl gate` includes a passing score-floor regression check; a killed-and-restarted nightly-pipeline run resumes cleanly instead of leaving partial state; a database blip during a run opens and closes a circuit instead of cascading; logs never contain a secret even when tailed live during a demo.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 35 | `T-106` | Regression gates and score floors in CI | 0.5 | 41.4h | `T-105`✓ |
| 36 | `T-122` | Partial-failure policy, retry and deadline alerting | 1.5 | 41.6h | `T-121`✓ |
| 37 | `T-142` | Logging finalisation: redaction, sampling, rotation, retention | 1.5 | 41.9h | `T-005`✓ |
| 38 | `T-143` | Metrics, health, readiness and version endpoints | 1.5 | 42.1h | `T-142` |
| 39 | `T-149` | Graceful shutdown, startup self-checks, pool resilience | 1.0 | 42.3h | `T-143` |

*This is the one place this phase reaches into "observability" — narrowly, for the three failure cases `tasks.md §7 Table 2` names that a live demo can actually hit (a DB blip, a killed process mid-batch, clock skew) and for keeping secrets out of logs. `T-144` (distributed tracing) and `T-145` (SLO alert rules) need a real tracing/alerting backend to mean anything and stay deferred below.*

`✓` = already done (Phases 1–6, `T-001`–`T-023`, or pulled forward — all marked `[x]` in `tasks.md`).

### Still deferred — briefs intact in `tasks.md`, not queued here

Every requirement these tasks touch (`R-29`, `R-30` partially, `N-05`, `N-08`–`N-11`, and the live-traffic half of `N-02`/`N-12`) has no functional gap left uncovered — these are the parts of `spec.md` that describe how the product runs as shipped infrastructure across a fleet, not what it does for a user, and none of it is demonstrable on one laptop regardless of hours spent.

| Group | Tasks | Why it stays out |
|---|---|---|
| Connectors and external feeds | `T-123`–`T-127`, `T-129`–`T-131` | Real file-drop/REST/DB transports and news/industry/bureau adapters need external systems a laptop demo doesn't have. `T-128`'s *synthetic* generator is the one exception — it's in `8H` above. |
| Live-traffic model operations | `T-108` | Drift monitoring needs sustained production traffic to detect drift against; a static demo dataset has none. `T-107`'s registry/cards/approval path (the rest of `N-12`) is in `8E` above. |
| Tracing and alerting | `T-144`, `T-145` | Need a real tracing backend and an alert-routing target to mean anything; `T-142`/`T-143` (the parts of `N-02` that matter without one) are in `8I` above. |
| Performance and capacity | `T-146`, `T-147` | Load-testing and capacity remediation against realistic traffic volumes — not meaningful against a curated demo dataset. |
| Backup and restore | `T-148` | Production disaster-recovery tooling; no fleet to protect on a laptop. |
| Installers and release engineering | `T-151`–`T-160`, `T-171`–`T-173` | Windows/Linux installers, containers, SBOM, upgrade/rollback, property/mutation/authorization-matrix test depth, release-candidate assembly — explicitly what you asked to cut. |
| Documentation | `T-161`–`T-165` | Admin/user guides, API reference, runbook, ADR set — not a running feature. |
| Compliance and privacy | `T-166`–`T-170` | Data inventory, retention enforcement, erasure, compliance evidence packs, pentest support — regulatory process work, not a product feature. |
| Removed by earlier decision | `T-140`, `T-141` | Hindi/i18n, struck before this document existed; unchanged. |

### Budget for this phase

`39 tasks` · `55.5 ideal-days` · `~9.1h at this document's measured pace` · **cumulative 42.3h from the start of Phase 1**

Work order: **Phase 7 first** (wires and populates the already-built 95 tasks — nothing in Phase 8 is worth building against an empty queue), **then Phase 8** in the sub-phase order above (8A→8I already respects every internal dependency, including the new `T-115`/`T-117`/`T-118` rows inside `8D`, `T-134` inside `8F`, and the `T-142`→`T-143`→`T-149` chain inside `8I`). If time is short inside Phase 8, `8A`, `8C` and `8D` pay for themselves fastest: covenant admin depth, interface polish and the admin console are the parts a judge sees and touches directly, and `8D` is now the one place that closes out two full requirements (`R-26`, `R-27`) rather than leaving them partial. `8F`'s regulatory exports are the highest-leverage differentiator per hour for an Indian credit-risk audience specifically. `8I` is worth doing before a live presentation even if nothing else in this phase is: it's the difference between a crashed pipeline mid-demo staying crashed and it resuming cleanly.
