# tasks.md — Covenant Radar · the dispatchable backlog

**Companion to `spec.md` v1.0 and `plan.md`, 2026-08-30. 173 blocks, `T-001`…`T-173`. `T-001`-`T-023` are implemented; `T-140` and `T-141` are removed; **§2.3 is the build order to work from**, 70 tasks in six phases, verified so that no task is ever reached before its prerequisites exist.**

---

## §0 The briefing

*Read this once per session, then work from the task block alone.*

**The product.** Covenant Radar gives Indian commercial-credit teams daily, evidence-backed warnings 30, 60 and 90 days before a monitored covenant is projected to breach. Code computes the ratios, tests the covenants against their contractual terms, scores behavioural signals for persistence and materiality, projects a dated crossing with named drivers, simulates interventions and ranks urgency. A language model does exactly two things: it proposes structured covenant fields from a sanction letter, where code independently re-verifies every one before a human confirms it; and it writes the connecting prose of a warning memo over figures it is given and cannot change. Everything the product presents as true comes from a record or a calculation.

**The stack.** Python 3.12+ · FastAPI + Uvicorn · Pydantic v2 · SQLAlchemy 2.0 + Alembic · PostgreSQL 17 (production) and SQLite (development, tests, offline evaluation) · Jinja2 + vendored HTMX + hand-written CSS + vanilla JS + inline SVG · APScheduler · pypdf, pdfplumber, OCRmyPDF, python-docx, WeasyPrint · NumPy, pandas · a pluggable LLM provider layer over httpx and tenacity · argon2-cffi, authlib, pysaml2, cryptography, itsdangerous · structlog, prometheus-client, opentelemetry · pytest, hypothesis, mutmut, ruff, mypy, import-linter, bandit, pip-audit, playwright, axe-core. Exact pins live in `requirements.lock`. **No dependency is added by any task except the one that owns the lock file.** Nothing is fetched from a third-party origin at runtime.

**The tree.** One installable package at `src/covenant_radar/` with layers `web`/`api` → `services` → `domain` + `ports` ← adapters (`db`, `ai`, `ingestion`, `documents`, `notifications`, `scheduler`) and cross-cutting `config`, `security`, `audit`, `observability`. `tests/` by suite, `evaluation/` for the harness and examples, `deploy/` for installers, `docs/` for the documentation set, `config/` for templates, `var/` generated and ignored. `plan.md §4` is the full tree.

**The rule that matters most.** `covenant_radar.domain` imports nothing from any layer above it and no framework at all — not SQLAlchemy, not FastAPI, not the AI package, not a database session. That single contract is what keeps the mechanism auditable, it is enforced by `lint-imports` in the gate, and a task that needs to break it has misunderstood the task.

**The gate.**

```
python -m radarctl gate --fast     # local, before every hand-over
python -m radarctl gate            # full, at every phase gate
```

**Standing prohibitions, binding on all 173 tasks and never repeated in a block.** Do not read or write outside the paths the block names. Do not add, upgrade or remove a dependency. Do not skip the snapshot in §3 step 1, and do not work two blocks at once. Do not edit `spec.md`, `plan.md` or `problem.md`, and edit this document only to tick a task off in §2.3. Do not leave a `TODO`, a placeholder, a fake return, a commented-out call, a skipped test or a disabled lint rule. Do not write `[OPEN-NN]`, `[ASSUMED-NN]`, `PILOT`, `TARGET`, `ESTIMATED`, `MEASURED` or any other document marker into code, comments, log lines, test names or anything a user sees. Do not commit a secret, a generated document, a log, a screenshot containing real data, or any customer data. Do not weaken an authorization check, an audit write, a shape check or an accessibility property to make a test pass.

**Standing acceptance criteria, binding on all 173 tasks and never repeated in a block.** (1) The stated behaviour works in the stated order. (2) Every item under `Every case` produces exactly its named result, status, message and persistence effect. (3) The named tests exist, are not skipped, and pass. (4) Every `Run` command exits as stated and produces the named output or artefact. (5) Only files under `Files owned` changed. (6) `python -m radarctl gate --fast` is green locally and the full gate is green in CI. (7) No secret, placeholder, `TODO` or disabled check was introduced. (8) The block's own `Done when` is true.

**The refusal.** Where a block does not say enough to do the work without guessing, stop and print, and nothing else:

```
BRIEF INCOMPLETE — T-0NN: <the one fact that is missing>
```

That line is agent output only. It never appears in a file, a comment or a test name.

---

## §1 The contract sheet

Every contract a task may rely on is frozen in **`plan.md §6`** — `C-01`…`C-23` HTTP and API routes, `C-30`…`C-41` pure domain functions, `C-50`…`C-60` ports, `C-70`…`C-79` commands. A block names the contracts it implements or calls by id; **read those rows in `plan.md §6` before starting, and implement them exactly**. A contract that seems wrong is a refusal, not an improvisation.

Three standing answers, so no block re-derives them:

- **Identity.** Every request resolves to a principal or returns `401`. A principal without the named permission gets `403` naming it. A principal whose portfolio scope excludes the subject gets `404`, not `403`, so scope is not an enumeration oracle. Rate limits return `429` with `Retry-After`.
- **Errors.** One exception hierarchy in `core/errors.py`, mapped once to HTTP status codes and once to UI states. No handler invents a status code.
- **Time, money, identity of records.** Instants are timezone-aware UTC, rendered IST, obtained from the injected `Clock`. Money is `Decimal` with a unit column. Keys are UUIDv7 with a short human reference where users see them.

---

## §2 The build order

### 2.1 The rule this order guarantees

**No task below is blocked.** Every `Depends on` entry of every scoped task is satisfied either by `T-001`-`T-023`, which are already implemented, or by a task appearing **earlier in this list**. This was verified mechanically against every dependency edge in this document; the check reports zero violations. Work strictly top to bottom and you can never reach a block whose prerequisite does not exist.

Effort is each block's own `Days:` value converted at the measured rate of **34 ideal-days in 7 hours** (4.86/hour), then de-rated for density: 62% across the scoring mathematics and the AI layer, 80-85% elsewhere. `Cum` is cumulative de-rated wall-clock from the start of Phase 1.

### 2.2 Scope

| | Tasks | Ideal days | At measured pace | De-rated |
|---|--:|--:|--:|--:|
| **Phase 1-4 - working product floor** | 51 | 81.5 | 16.8h | 24.2h |
| Phase 1-5 - recommended target | 58 | 92.0 | 18.9h | 27.7h |
| Phase 1-6 - full scoped set | 70 | 111.0 | 22.9h | 33.1h |

**Removed from the product.** `T-140` (translation catalogues and the extraction build check) and `T-141` (Hindi translation and locale formatting) are struck. Only `T-141` and `T-159` ever referenced them; `T-141` is itself removed and `T-159` is out of scope, narrowing to themes-only if it returns. **No task in this build order depends on either.** The dormant `src/covenant_radar/i18n/` scaffold from the implemented `T-022` is left untouched and inert; English strings render directly.

**Out of scope for this window: 78 blocks**, listed in §2.4. Their briefs are unchanged and nothing was deleted, so reintroducing any of them is a re-queue rather than a rebuild.

### 2.3 The order


#### Phase 1 - Covenant core computes

`11 tasks` · `18.5 ideal-days` · `~4.5h this phase` · **cumulative 4.5h**

**Phase gate - demonstrate before starting the next phase:** A signed covenant tests correctly against imported statements, with exceptions, waivers, cure, staleness and not-computable all exact.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 1 | [x] `T-024` | Statement chart of accounts and the normalisation model | 1.5 | 0.4h | `T-010`* |
| 2 | [x] `T-027` | Ratio library part 1 — leverage, coverage and liquidity | 1.5 | 0.7h | `T-024` |
| 3 | [x] `T-028` | Ratio library part 2 — conduct, working capital and covenant conditions | 1.5 | 1.1h | `T-027` |
| 4 | [x] `T-030` | Not-computable and missing-line behaviour across the library | 0.5 | 1.2h | `T-028` |
| 5 | [x] `T-031` | Covenant registry: model, versioning, immutability enforcement | 2.0 | 1.7h | `T-010`* |
| 6 | [x] `T-032` | Exceptions, waivers, cure and grace periods | 1.5 | 2.1h | `T-031` |
| 7 | [x] `T-033` | Registry service, maker-checker path and API | 1.5 | 2.4h | `T-032`, `T-018`* |
| 8 | [x] `T-034` | Covenant engine: evaluation, headroom, verdicts, boundaries | 2.5 | 3.0h | `T-031`, `T-030` |
| 9 | [x] `T-037` | Stage-2 trace rows and engine explainability data | 1.5 | 3.4h | `T-034` |
| 10 | [x] `T-040` | Reference portfolio: borrowers, facilities, financials | 2.5 | 4.0h | `T-011`* |
| 11 | [x] `T-041` | Reference portfolio: cohorts, signals and labelled outcomes | 2.0 | 4.5h | `T-040` |

#### Phase 2 - AI spine, documents and grounded intake

`13 tasks` · `20.5 ideal-days` · `~6.8h this phase` · **cumulative 11.3h**

**Phase gate - demonstrate before starting the next phase:** A sanction letter becomes proposed covenant fields; one clause is struck with its failing check named; the rest confirm and test on the spot. Runs offline against cassettes.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 12 | [x] `T-088` | LLM provider protocol and the four adapters | 2.0 | 5.1h | `T-004`* |
| 13 | [x] `T-089` | The single call site: retries, timeouts, ceilings, budget, logging | 1.5 | 5.6h | `T-088` |
| 14 | [x] `T-090` | Outbound masking whitelist that fails closed | 1.5 | 6.1h | `T-089` |
| 15 | [x] `T-091` | Recorded-response adapter and cassette management | 0.5 | 6.3h | `T-088` |
| 16 | [x] `T-092` | Prompt files, version binding and the build check | 1.0 | 6.6h | `T-089` |
| 17 | [x] `T-084` | Document model, upload, virus scan, encrypted store | 2.0 | 7.3h | `T-017`*, `T-019`* |
| 18 | [x] `T-085` | Native PDF text and span extraction | 2.0 | 8.0h | `T-084` |
| 19 | [x] `T-086` | OCR pipeline, page confidence, human-review routing | 2.0 | 8.6h | `T-085` |
| 20 | [x] `T-087` | Document classification and the span-highlighting viewer | 1.5 | 9.1h | `T-086`, `T-021`* |
| 21 | [x] `T-093` | Clause candidate detection over documents and text | 1.5 | 9.6h | `T-085` |
| 22 | [x] `T-094` | Stage-1 proposal, parsing and normalisation | 1.5 | 10.1h | `T-093`, `T-092` |
| 23 | [x] `T-095` | The six code verifications, failing closed | 2.0 | 10.8h | `T-094`, `T-034` |
| 24 | [x] `T-096` | Intake service, confirm refusal and the approval flow | 1.5 | 11.3h | `T-095`, `T-033` |

#### Phase 3 - Evidence ledger, 30/60/90 forecast and drivers

`17 tasks` · `24.5 ideal-days` · `~8.1h this phase` · **cumulative 19.4h**

**Phase gate - demonstrate before starting the next phase:** A transient blip decays while a sustained pattern escalates to a dated crossing with named drivers and a visible confidence.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 25 | [x] `T-042` | Signal event model and the ingestion framework | 2.0 | 12.0h | `T-010`* |
| 26 | [x] `T-046` | Evidence item model and derivation from events | 1.5 | 12.5h | `T-042` |
| 27 | [x] `T-047` | Persistence scoring | 1.5 | 12.9h | `T-046`, `T-012`* |
| 28 | [x] `T-048` | Materiality scoring | 1.5 | 13.4h | `T-047`, `T-034` |
| 29 | [x] `T-049` | Decay and visibility | 1.0 | 13.8h | `T-047` |
| 30 | [x] `T-050` | Supersession and revision | 1.5 | 14.3h | `T-049` |
| 31 | [x] `T-051` | Stage-3 trace rows and ledger explainability | 1.0 | 14.6h | `T-050` |
| 32 | [x] `T-052` | Trend projection and the daily path | 2.0 | 15.3h | `T-034`, `T-050` |
| 33 | [x] `T-053` | Threshold crossing and dating | 1.5 | 15.8h | `T-052` |
| 34 | [x] `T-054` | Probability mapping, clamping and term capture | 1.5 | 16.3h | `T-053` |
| 35 | [x] `T-055` | Confidence model from completeness, support and staleness | 1.5 | 16.8h | `T-054` |
| 36 | [x] `T-056` | Forecast persistence, runs, versioning and staleness marking | 1.5 | 17.3h | `T-055` |
| 37 | [x] `T-057` | Driver attribution and normalisation | 1.5 | 17.8h | `T-056` |
| 38 | [x] `T-058` | Attribution links to evidence and stage-4 trace | 1.0 | 18.1h | `T-057` |
| 39 | [x] `T-059` | Urgency, banding and the deterministic tie-break | 1.5 | 18.6h | `T-056` |
| 40 | [x] `T-060` | What-changed computation between runs | 1.0 | 18.9h | `T-059` |
| 41 | [x] `T-061` | Queue query, filtering and the saved-view model | 1.5 | 19.4h | `T-060`, `T-016`* |

#### Phase 4 - Audit chain and screens  ***WORKING PRODUCT***

`10 tasks` · `18.0 ideal-days` · `~4.8h this phase` · **cumulative 24.2h**

**Phase gate - demonstrate before starting the next phase:** Queue to case file to 30/60/90 with drivers; every figure resolves through the why-panel; the intake screen refuses to render a confirm control on a failed proposal.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 42 | [x] `T-066` | Audit store, hash chain and append-only enforcement | 2.0 | 19.9h | `T-010`* |
| 43 | [x] `T-067` | Audit emission across every service, with the coverage test | 1.5 | 20.3h | `T-066` |
| 44 | [x] `T-068` | Warning reconstruction assembly | 1.5 | 20.7h | `T-067`, `T-058` |
| 45 | [x] `T-070` | Trace model, the unified stage record and its reader | 1.5 | 21.1h | `T-066` |
| 46 | [x] `T-071` | Why-panel rendering for code, model and statistical stages | 2.0 | 21.6h | `T-070`, `T-021`* |
| 47 | [x] `T-073` | Portfolio queue screen | 2.0 | 22.1h | `T-061`, `T-021`* |
| 48 | [x] `T-075` | Case file: layout, header facts, covenant strip | 2.0 | 22.6h | `T-073` |
| 49 | [x] `T-076` | Forecast panel and inline SVG trajectories | 1.5 | 23.0h | `T-075`, `T-056` |
| 50 | [x] `T-077` | Horizon control: interaction, keyboard, reduced motion, stops fallback | 2.0 | 23.5h | `T-076` |
| 51 | [x] `T-097` | Intake screen: side-by-side, inline verdicts, hand entry | 2.0 | 24.2h | `T-096`, `T-087` |

#### Phase 5 - Intervention simulation and the memo

`7 tasks` · `10.5 ideal-days` · `~3.5h this phase` · **cumulative 27.7h**

**Phase gate - demonstrate before starting the next phase:** Three interventions compared against doing nothing, and a grounded memo whose every figure resolves to a record.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 52 | [x] `T-062` | Intervention effect models and applicability rules | 1.5 | 24.7h | `T-052` |
| 53 | [x] `T-063` | Counterfactual simulation and multi-option comparison | 2.0 | 25.4h | `T-062` |
| 54 | [x] `T-064` | Simulation persistence and assumption capture | 1.0 | 25.7h | `T-063` |
| 55 | [x] `T-098` | Action catalogue: model, management and applicability | 1.5 | 26.2h | `T-062` |
| 56 | [x] `T-099` | Memo slot assembly from records only | 1.5 | 26.7h | `T-058`, `T-064` |
| 57 | [x] `T-100` | Stage-7 prompt, drafting and the four shape checks | 2.0 | 27.4h | `T-099`, `T-092` |
| 58 | [x] `T-101` | Memo refusal, retry and persistence rules | 1.0 | 27.7h | `T-100` |

#### Phase 6 - Drawdown, only if time remains

`12 tasks` · `19.0 ideal-days` · `~5.4h this phase` · **cumulative 33.1h**

**Phase gate - demonstrate before starting the next phase:** Taken strictly in the order below. Each entry is a clean re-queue of an untouched block.

| # | Task | Title | Days | Cum | Depends on |
|--:|---|---|--:|--:|---|
| 59 | [x] `T-120` | Job model, scheduler, run ledger, restart resumption | 2.0 | 28.2h | `T-010`* |
| 60 | [x] `T-121` | Nightly pipeline composition and idempotent re-run | 2.0 | 28.7h | `T-120`, `T-060` |
| 61 | [x] `T-103` | Evaluation example schema and the authored set | 2.0 | 29.4h | `T-041` |
| 62 | [x] `T-104` | Evaluation harness: the product arm | 2.0 | 30.1h | `T-103`, `T-091` |
| 63 | [x] `T-105` | Evaluation harness: the baseline arm and the scoreboard | 1.5 | 30.5h | `T-104` |
| 64 | [x] `T-102` | Memo PDF and DOCX export with integrity hash | 1.5 | 31.0h | `T-101` |
| 65 | [x] `T-069` | Evidence bundle export, manifest and verification | 1.5 | 31.4h | `T-068` |
| 66 | [x] `T-080` | Audit search and reconstruction screens | 1.5 | 31.8h | `T-069` |
| 67 | [x] `T-079` | Simulator screen and comparison view | 1.5 | 32.2h | `T-064`, `T-076` |
| 68 | [x] `T-111` | Override capture and view revision | 1.5 | 32.6h | `T-067`, `T-071` |
| 69 | [x] `T-074` | Queue filters, saved views and bulk selection | 1.0 | 32.8h | `T-073` |
| 70 | [x] `T-072` | Why-panel API and the no-JavaScript full page | 1.0 | 33.1h | `T-071` |

`*` = already implemented in `T-001`-`T-023`.

### 2.4 Out of scope for this window

Deferred, briefs intact: connectors and feeds (`T-123`-`T-131`), regulatory reporting (`T-132`-`T-134`), the public REST API and search (`T-135`-`T-139`), notifications and case workflow (`T-109`, `T-110`, `T-112`-`T-119`, `T-122`), covenant admin depth (`T-025`, `T-026`, `T-029`, `T-036` — `T-035`, `T-038` and `T-039` have since been implemented out of order), signal plumbing and calibration (`T-043`-`T-045`, `T-065`), model governance (`T-106`-`T-108`), interface polish (`T-081`-`T-083`), and the whole of observability, performance, backup, installers, extended testing, documentation, compliance and release engineering (`T-142`-`T-173`).

**If time remains after Phase 6, restore in this order:** `T-025` statement import (0.6h) · `T-036` SMA banding (0.3h) · `T-065` threshold calibration (1.0h) · `T-135`+`T-136` REST API and OpenAPI (1.0h).

### 2.5 Ordering rules

A task starts only when every task in its `Depends on` is **finished and its gate command green**, not merely written. Take them in the printed order. A task that exceeds its estimate by more than half is stopped and split, and the split is recorded in `MERGE_LOG.md`. Do not begin a phase before the previous phase gate has been demonstrated. Re-forecast the remaining phases from the observed ratio at the end of Phase 2 and Phase 4.

### 2.6 The longest dependent run

`T-024 -> T-027 -> T-028 -> T-030 -> T-034 -> T-052 -> T-053 -> T-054 -> T-055 -> T-056 -> T-059 -> T-060 -> T-061 -> T-073 -> T-075 -> T-076 -> T-077` - **27.0 ideal-days of strictly dependent work**, almost all of it inside Phase 3. A task on this run that stalls delays everything after it; a task off it does not. Phase 3 is the one to protect.

---

## §3 How to work a block

1. **Snapshot** before touching anything, so the task is reversible:

```
robocopy src   var\snapshots\T-0NN\src   /MIR /NFL /NDL /NJH /NJS
robocopy tests var\snapshots\T-0NN\tests /MIR /NFL /NDL /NJH /NJS
```

2. **Read** the block and only the paths its `Read first` names, plus `plan.md §6` for the contracts it lists.
3. **Build** inside `Files owned`. Write the named tests as you go.
4. **Prove** with `python -m radarctl gate --fast`, then each `Run` command with its stated expected result.
5. **Record** in `MERGE_LOG.md`: task id, snapshot path, planned hours, actual hours, and anything the block did not anticipate.
6. **Move on** to the next numbered task in §2.3. At a phase boundary, run the full `python -m radarctl gate` and demonstrate the phase gate first.

**To undo a task:** restore its snapshot.

```
robocopy var\snapshots\T-0NN\src   src   /MIR /NFL /NDL /NJH /NJS
robocopy var\snapshots\T-0NN\tests tests /MIR /NFL /NDL /NJH /NJS
```

`var/` is ignored and never packaged, so snapshots cost nothing at build time. Take the snapshot **before** the task, not after - it is the state you want back if the task goes wrong.

**Recovering an interrupted task:** write into `MERGE_LOG.md` what is done, what is next and which command last passed, then resume from the block. Never guess what a previous session intended.

## §4 The phase gates

| Gate | Demonstrated by | Evidence recorded |
|---|---|---|
| Phase 1 | A covenant entered by hand tests correctly against imported statements; exceptions, waivers, cure, staleness and not-computable exact; every hand-worked ratio case exact | Test transcript, the hand-worked case table with actual values |
| Phase 2 | A sanction letter through extraction and verification with one clause deliberately struck, the rest confirmed and tested; the provider layer running offline against cassettes | Intake capture showing a struck proposal with its failing check named |
| Phase 3 | Reference-portfolio cohort report: deteriorating dated within tolerance, noisy never escalating, stable below amber, attribution shares summing | Cohort report, trace samples showing every threshold with its side |
| Phase 4 | Primary flow end to end in a browser and by keyboard; every case-file figure resolving through the why-panel; a warning reconstructing from the audit chain | Screenshots at three viewports, keyboard walkthrough, a reconstruction |
| Phase 5 | Three interventions simulated side by side against doing nothing; a memo generating, grounding and refusing | Simulation comparison, memo with every figure traced, refusal transcript |
| Phase 6 | Whatever was reached, demonstrated in the order taken | Per-task evidence in `MERGE_LOG.md` |

A gate is demonstrated, never asserted: someone runs it, someone else witnesses it, and the evidence is attached in `MERGE_LOG.md`.

**Phase 4 is the floor.** If the clock stops there you have a complete, auditable, browsable product. Phases 5 and 6 add depth, not viability.

## §5 The tasks

> **Read §2.3 for the order to build in.** The `M-0`…`M-6` headings below are the original requirement groupings and are kept so each block keeps its context; they are **not** the build sequence. Every block carries a `Build:` field giving its position in §2.3, or marking it `DEFERRED` or `REMOVED`.

---

### M-0 · Foundation — 34.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-001` · Repository, packaging, lock file and tooling baseline
`Milestone: M-0` · `Supports: plan §2, §4` · `Days: 1.0` · `Depends on: —` · `Snapshot: var/snapshots/T-001/` · `Build: DONE`

**Goal:** an installable package, an exact dependency lock, the tool configuration every later task's definition of done references, and a merge ledger that supersedes the previous plan's.
**Context:** the working directory holds `spec.md`, `plan.md`, `tasks.md`, `problem.md`, a hackathon-era `MERGE_LOG.md`, `.gitignore` and `.claude/`. There is no source, no manifest and no test. There is no version-control system on this machine; §3's snapshot protocol is the reversibility mechanism and `MERGE_LOG.md` is the ledger.
**Read first:** `plan.md §2` (the pinned table), `plan.md §4` (the tree).
**Contracts:** none.
**Files owned:** `pyproject.toml`, `requirements.lock`, `.python-version`, `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `LICENSE`, `.gitignore`, `.gitattributes`, `.editorconfig`, `MERGE_LOG.md`, `src/covenant_radar/__init__.py`, `src/covenant_radar/cli.py`, `tests/__init__.py`
**Behaviour:** `pip install -e .` succeeds from a clean virtual environment; `python -c "import covenant_radar; print(covenant_radar.__version__)"` prints a semantic version; `python -m radarctl --help` lists the command groups from `plan.md §6.4` as stubs that exit 2 with "not yet implemented" and name the task that implements each.
**Every case:** a pin absent from the registry → stop, name the pin, do not substitute; the registry unreachable → stop, this is an environment problem, not a code problem; an existing file about to be overwritten → only the four documents and `.gitignore` pre-exist, and `.gitignore` is extended rather than replaced.
**Steps:** 1. Create `var/snapshots/` and take the Phase 1 baseline snapshot per §3. 2. Write `pyproject.toml` with a `hatchling` backend, the `src` layout, the console entry point `radarctl`, and tool sections for ruff, mypy, pytest, coverage and import-linter kept as placeholders `T-002` fills. 3. Pin every dependency from `plan.md §2` exactly and generate `requirements.lock`. 4. Write `src/covenant_radar/__init__.py` with `__version__` as the single source of truth, and `cli.py` with the stub command groups. 5. Write `.gitignore` covering `var/`, `.venv/`, caches, `*.db`, `.env`, `playwright-report/`, `.coverage*`. 6. Write `README.md` (what it is, how to run it, where the docs are), `CONTRIBUTING.md` (the §3 loop verbatim), `CHANGELOG.md` and `LICENSE`. 7. Replace `MERGE_LOG.md` with the new ledger: the supersession note naming the previous plan's task ids, the git result, and the empty per-task and per-gate table headers.
**Tests:** `tests/unit/test_packaging.py` — `test_version_importable`, `test_cli_entry_point_exists`, `test_lock_has_no_ranges`, `test_gitignore_covers_generated_paths`.
**Run:** `pip install -e .` exit 0 · `python -m radarctl --help` exit 0 listing the groups · `pytest -q tests/unit/test_packaging.py` 4 passed.
**Done when:** a clean virtual environment installs the package, the version imports, the lock contains no version range, and `MERGE_LOG.md` records the supersession and the git result.
**Evidence:** the install transcript, the lock file, the `MERGE_LOG.md` header block.

---

### [x] `T-002` · Quality gate: format, lint, types, import contracts, pre-commit
`Milestone: M-0` · `Supports: plan §0, N-09` · `Days: 1.0` · `Depends on: T-001` · `Snapshot: var/snapshots/T-002/` · `Build: DONE`

**Goal:** one command proves the tree, and the layer contract that makes the mechanism auditable is machine-enforced from the first day rather than the last.
**Context:** `plan.md §3.1` states six import contracts. Contract 1 — `covenant_radar.domain` imports nothing above it and no framework — is the product's central property. Enforce all six now, while the packages are empty and the contracts are free, rather than after they are violated.
**Read first:** `plan.md §0` (the gate steps), `plan.md §3.1` (the six contracts), `pyproject.toml`.
**Contracts:** `C-75` `radarctl gate [--fast]`.
**Files owned:** `pyproject.toml` (tool sections only), `.pre-commit-config.yaml`, `noxfile.py`, `src/covenant_radar/cli.py` (the `gate` command), `.importlinter`, `tests/unit/test_gate.py`
**Behaviour:** `python -m radarctl gate --fast` runs format check, lint, type-check, import contracts, unit and property tests, and Alembic drift, stopping at the first failure and exiting with that step's code. The full gate adds integration, contract, evaluation, end-to-end, accessibility, security scanning and performance steps, each skipping with a printed `SKIP <step> — not yet implemented` and a zero exit while its implementing task is outstanding, so the gate is runnable from today.
**Every case:** a step's tool absent → fail naming the tool, never skip silently; a step not yet implemented → `SKIP` with the implementing task id; any step failing → stop there and exit with its code; `--fast` selecting a step that does not exist → fail with the list of valid steps.
**Steps:** 1. Configure ruff (format and lint, line length 100, the rule set including a ban on bare `datetime.now()` and on `print`), mypy (strict for `domain` and `services`, standard elsewhere), pytest (markers per suite, strict markers, `-ra`), coverage (fail-under thresholds set to zero for now with the target recorded in a comment `T-160` raises). 2. Write `.importlinter` with the six contracts as layered and forbidden-module rules. 3. Write `noxfile.py` with one session per gate step so each is runnable alone and identically in CI. 4. Implement `radarctl gate` calling the sessions in `plan.md §0`'s order with `--fast` selecting the first six. 5. Write `.pre-commit-config.yaml` with format, lint, type-check and a `detect-secrets` hook.
**Tests:** `tests/unit/test_gate.py` — `test_gate_runs_steps_in_order`, `test_gate_stops_at_first_failure`, `test_unimplemented_step_skips_with_task_id`, `test_import_contracts_parse`, `test_domain_contract_would_fail_on_framework_import` (writes a temporary offending module and asserts the contract catches it).
**Run:** `python -m radarctl gate --fast` exit 0 · `lint-imports` exit 0 · `pytest -q tests/unit/test_gate.py` 5 passed · `pre-commit run --all-files` exit 0.
**Done when:** the fast gate is green, all six import contracts are declared and enforced, and a deliberately offending domain import fails the contract check.
**Evidence:** the gate transcript, the contract file, the deliberate-violation test output.

---

### [x] `T-003` · CI pipeline with offline-capable gates and artefact publishing
`Milestone: M-0` · `Supports: N-09` · `Days: 1.0` · `Depends on: T-002` · `Snapshot: var/snapshots/T-003/` · `Build: DONE`

**Goal:** every change is proved by the same gate on a clean machine with no network access, and the evidence a milestone gate needs is published as an artefact rather than reconstructed by hand.
**Context:** every test suite must run offline — a property of the design, not a testing convenience, because it is the same mechanism that lets the product run air-gapped. CI's PostgreSQL instance is `plan.md [OPEN-12]`; take its default (a service instance) and mark the integration suite as requiring it, failing rather than silently skipping when it is absent.
**Read first:** `noxfile.py`, `pyproject.toml`.
**Contracts:** `C-75`.
**Files owned:** `.github/workflows/ci.yml`, `.github/workflows/security.yml`, `.github/workflows/release.yml`, `docs/adr/0001-ci-and-offline-gates.md`
**Behaviour:** the CI workflow installs from the lock, starts PostgreSQL, runs the full gate, and publishes coverage, the evaluation scoreboard, screenshots, the performance table and the accessibility report as artefacts. The security workflow runs dependency and secret scanning on a schedule and on every change. The release workflow builds, generates the bill of materials and attaches the evidence pack.
**Every case:** the network reachable but a test attempting an outbound call → the test fails, because an outbound-blocking fixture is installed globally; PostgreSQL unavailable → the integration job fails naming it, never skips; a scan finding a vulnerability at or above the configured severity → the build fails with the advisory named.
**Steps:** 1. Write the CI workflow with jobs for fast gate, integration, evaluation, browser and scanning, each cacheable and each running from the lock. 2. Install a session-scoped fixture in `tests/conftest.py` — created here, extended later — that patches socket creation to raise for any non-local address, so an accidental outbound call is a test failure. 3. Publish artefacts by name. 4. Write the security and release workflows. 5. Record the offline-by-default decision as `ADR-0001`.
**Tests:** `tests/unit/test_offline_guard.py` — `test_outbound_socket_raises`, `test_loopback_allowed`.
**Run:** `pytest -q tests/unit/test_offline_guard.py` 2 passed · the CI workflow green · `nox -l` lists every gate session.
**Done when:** CI runs the full gate on a clean runner with the outbound guard active and publishes every named artefact.
**Evidence:** a green CI run with its artefact list, `ADR-0001`.

---

### [x] `T-004` · Typed settings, precedence, startup validation and capabilities
`Milestone: M-0` · `Builds: N-03` · `Days: 1.5` · `Depends on: T-001` · `Snapshot: var/snapshots/T-004/` · `Build: DONE`

**Goal:** one typed settings object, no secret in any file, a startup that refuses rather than starts wrong, and an explicit record of which capabilities are configured so every degraded state is designed rather than discovered.
**Context:** `spec §11.1` requires the product to work with no outbound network, with model-using stages degrading gracefully. That is a *capability* switch reflecting what is configured, never a feature flag that changes product behaviour, and it may never enable anything `spec §8.2` forbids.
**Read first:** `plan.md §3.3` (configuration), `plan.md §2` (what needs configuring), `config/` (empty).
**Contracts:** `C-70`'s refusal behaviour.
**Files owned:** `src/covenant_radar/config/settings.py`, `src/covenant_radar/config/capabilities.py`, `config/default.toml`, `config/production.example.toml`, `.env.example`, `tests/unit/test_settings.py`
**Behaviour:** settings load from defaults, then the configuration file, then environment variables, with environment winning; every secret comes only from the environment or the OS keyring and never from a file; the object is validated once at import and immutable thereafter; `Capabilities` reports for each of model provider, SSO, OCR, SMTP, webhooks and document store whether it is configured, and every consumer asks rather than assumes.
**Every case:** an invalid value → refuse to start, naming the key, the file and the line, with the allowed values; a missing required secret → refuse to start naming the environment variable, never a default; a secret found in a configuration file → refuse to start, because that is a security defect and not a convenience; an unknown key in the file → refuse, so a typo is not silently ignored; a capability unconfigured → reported as such, never an exception at the point of use.
**Steps:** 1. Define the settings model with nested sections for database, security, documents, ai, notifications, ingestion, observability and web. 2. Implement precedence and the file loader with an unknown-key check. 3. Implement secret sourcing from environment and keyring, with a check that refuses secrets present in files. 4. Implement `Capabilities` derived from the loaded settings. 5. Write `config/default.toml`, `config/production.example.toml` and `.env.example` listing every variable with no value.
**Tests:** `tests/unit/test_settings.py` — `test_precedence_env_over_file`, `test_invalid_value_names_key_file_and_line`, `test_missing_secret_refuses_start`, `test_secret_in_file_refuses_start`, `test_unknown_key_refuses`, `test_capabilities_reflect_configuration`, `test_settings_immutable_after_load`.
**Run:** `pytest -q tests/unit/test_settings.py` 7 passed · `python -c "from covenant_radar.config.settings import get_settings; get_settings()"` exit 0 with the example configuration.
**Done when:** all seven tests pass, no secret can be read from a file, and every capability is reported rather than assumed.
**Evidence:** the test output, `.env.example`.

---

### [x] `T-005` · Core: errors, identifiers, clock, money, context, structured logging
`Milestone: M-0` · `Supports: plan §3.3` · `Days: 1.5` · `Depends on: T-004` · `Snapshot: var/snapshots/T-005/` · `Build: DONE`

**Goal:** the primitives every later layer depends on, decided once — one exception hierarchy, one identifier scheme, one injectable clock, one money type, one request context, one log shape.
**Context:** `plan.md §3.3` fixes each of these. The `Clock` port exists so tests are deterministic; a lint rule already bans bare `datetime.now()`, and this task provides the thing to use instead.
**Read first:** `plan.md §3.3`, `src/covenant_radar/config/settings.py`.
**Contracts:** `C-59` `Clock.now()`.
**Files owned:** `src/covenant_radar/core/errors.py`, `ids.py`, `clock.py`, `money.py`, `result.py`, `context.py`, `src/covenant_radar/observability/logging.py`, `config/logging.toml`, `tests/unit/test_core.py`
**Behaviour:** `DomainError`, `ValidationError`, `AuthorizationError`, `NotFound`, `Conflict`, `ExternalServiceError` with a code, a message and an optional field path; `new_id()` returning UUIDv7 and `human_reference(prefix, sequence)` returning `B-000123`; `Clock` protocol with a system implementation and a fixed test implementation; `Money` as `Decimal` with a unit, refusing float construction and refusing arithmetic across units; a `request_id` context variable; and a structlog configuration emitting JSON with the request id on every line and redacting configured key patterns.
**Every case:** `Money` constructed from a float → `TypeError` naming the value, because floating-point money is a defect, not a style preference; arithmetic across different units → `ValueError`; a log value matching a secret or personal-class pattern → redacted before writing; a log call outside a request context → written with a null request id rather than raising; two identifiers generated in the same microsecond → distinct and monotonically ordered.
**Steps:** 1. Write the exception hierarchy with codes. 2. Write `ids.py` with UUIDv7 and the reference formatter. 3. Write `clock.py` with the protocol, the system clock and `FixedClock`. 4. Write `money.py` with `Decimal` construction, unit handling and the float refusal. 5. Write `context.py` with the request and job context variables. 6. Configure structlog with the JSON renderer, the context processor and the redaction processor.
**Tests:** `tests/unit/test_core.py` — `test_error_codes_unique`, `test_uuid7_monotonic`, `test_human_reference_format`, `test_fixed_clock_deterministic`, `test_money_refuses_float`, `test_money_refuses_cross_unit_arithmetic`, `test_log_carries_request_id`, `test_log_redacts_secret_pattern`, `test_log_outside_context_does_not_raise`.
**Run:** `pytest -q tests/unit/test_core.py` 9 passed · `python -c "from covenant_radar.observability.logging import configure; configure(); import structlog; structlog.get_logger().info('x')"` prints one JSON line.
**Done when:** all nine tests pass and no module in the tree calls `datetime.now()` directly.
**Evidence:** the test output, one sample log line.

---

### [x] `T-006` · Database engine, session, unit of work, repository base
`Milestone: M-0` · `Builds: R-01` · `Days: 1.5` · `Depends on: T-005` · `Snapshot: var/snapshots/T-006/` · `Build: DONE`

**Goal:** one data-access foundation that works identically on PostgreSQL and SQLite, with transactions opened by services and never by repositories, and a repository base whose read methods cannot forget the caller's scope.
**Context:** `plan.md §5`'s conventions — UUIDv7 keys stored as `uuid` on PostgreSQL and `char(36)` on SQLite through one custom type, aware UTC timestamps, `numeric` money, `jsonb` or checked `text` payloads. `plan.md §3.3`: one unit of work per use case. `spec §11.1`: SQLite must run the same models so development and the offline evaluation harness need no server.
**Read first:** `plan.md §5` (conventions), `plan.md §3.1` (contract 5: nothing outside `db` imports a session), `src/covenant_radar/config/settings.py`.
**Contracts:** `C-57` `UnitOfWork`, `C-58` `Repository[T]`.
**Files owned:** `src/covenant_radar/db/base.py`, `session.py`, `types.py`, `src/covenant_radar/ports/unit_of_work.py`, `ports/repository.py`, `tests/unit/test_db_base.py`, `tests/integration/conftest.py`
**Behaviour:** an engine factory per configured URL with pooling and pre-ping; a `UnitOfWork` context manager committing on success and rolling back on any exception; a declarative base carrying the standard columns; portable custom types for UUID, aware datetime, money and JSON; and a generic repository base whose `find` and `list` require a scope argument at the type level so a caller cannot omit it.
**Every case:** a nested unit of work → `RuntimeError` naming both call sites, because it is a programming error; a database unreachable → `ExternalServiceError` naming the host and not the credentials; a repository read without a scope → a type error at check time and a `TypeError` at run time; an aware datetime written and read back → identical instant on both engines; a `Decimal` written and read back → identical value and scale on both engines.
**Steps:** 1. Write `types.py` with the four portable types and their round-trip behaviour. 2. Write `base.py` with the declarative base, the standard column mixin and the naming convention for constraints and indexes. 3. Write `session.py` with the engine factory, session factory and `UnitOfWork`. 4. Write the `Repository` protocol with scope-carrying reads and a base implementation. 5. Write `tests/integration/conftest.py` with per-test transactional isolation against a real PostgreSQL instance.
**Tests:** `tests/unit/test_db_base.py` — `test_uuid_type_round_trip_both_engines`, `test_datetime_type_is_aware_utc`, `test_money_type_preserves_scale`, `test_nested_unit_of_work_raises`, `test_uow_rolls_back_on_exception`, `test_repository_read_requires_scope`.
**Run:** `pytest -q tests/unit/test_db_base.py` 6 passed · `pytest -q tests/integration -k db_base` green against PostgreSQL.
**Done when:** the six tests pass on both engines and no module outside `covenant_radar.db` imports a session, proven by the import contract.
**Evidence:** the dual-engine test output.

---

### [x] `T-007` · Model: organisation, portfolio, user, role, permission, session, API key
`Milestone: M-0` · `Builds: R-01` · `Days: 1.5` · `Depends on: T-006` · `Snapshot: var/snapshots/T-007/` · `Build: DONE`

**Goal:** the identity and structure tables exactly as `plan.md §5.1` defines them, including the constraint that makes maker-checker impossible to bypass in the database.
**Context:** `plan.md §5.1` is the field-level definition; copy it exactly. The scoping tree uses a materialised path so the hot-path predicate is one indexed prefix match rather than a recursive query. `user_portfolio_scope` absence means **no access**, never all access — a default that fails closed.
**Read first:** `plan.md §5.1`, `src/covenant_radar/db/base.py`, `types.py`.
**Contracts:** none directly; the tables `C-17` and `C-58` operate on.
**Files owned:** `src/covenant_radar/db/models/organisation.py`, `portfolio.py`, `identity.py`, `maker_checker.py`, `tests/unit/test_model_identity.py`
**Behaviour:** every table, column, type, nullability, unique constraint and index from `plan.md §5.1`, with `maker_checker_request` carrying a database check constraint that `maker_id <> checker_id`.
**Every case:** two users with the same username → unique violation surfaced as `Conflict` naming the field; a scope row for a non-existent portfolio → foreign-key violation, never a dangling scope; a maker-checker row where maker equals checker → check-constraint violation surfaced as `Conflict` naming the constraint; a portfolio path deeper than the configured maximum → `ValidationError`, so the path column cannot silently truncate.
**Steps:** 1. Write the organisation and portfolio models with the materialised path and its maintenance on insert and move. 2. Write user, role, permission, role-permission, user-role and user-portfolio-scope. 3. Write session and API key with hashed tokens only — a plain token is never stored. 4. Write the maker-checker request model with the distinct-actor check constraint. 5. Add every index `plan.md §5.1` names.
**Tests:** `tests/unit/test_model_identity.py` — `test_all_columns_match_plan`, `test_username_unique`, `test_portfolio_path_maintained_on_move`, `test_maker_equals_checker_rejected_by_constraint`, `test_session_stores_only_token_hash`, `test_scope_absence_means_no_access`.
**Run:** `pytest -q tests/unit/test_model_identity.py` 6 passed.
**Done when:** the six tests pass and the distinct-actor constraint is enforced by the database, not only by application code.
**Evidence:** the test output, the generated DDL for `maker_checker_request`.

---

### [x] `T-008` · Model: borrower, group, related party, contact, facility, conduct
`Milestone: M-0` · `Builds: R-01, R-02` · `Days: 1.5` · `Depends on: T-007` · `Snapshot: var/snapshots/T-008/` · `Build: DONE`

**Goal:** the borrower and facility tables with effective dating, encrypted identity columns and the deterministic fingerprint that lets CIN be unique and searchable without being readable.
**Context:** `plan.md §5.2`. `cin_enc` and `pan_enc` are field-encrypted (`T-017` supplies the type; this task declares the columns against its interface); `cin_fingerprint` is an HMAC so uniqueness and lookup work without decryption and the column is not a rainbow-table target. Facilities are effective-dated: a limit change inserts a new row and closes the old.
**Read first:** `plan.md §5.2`, `src/covenant_radar/db/models/identity.py`, `types.py`.
**Contracts:** the tables `C-21`'s borrower and facility resources return.
**Files owned:** `src/covenant_radar/db/models/borrower.py`, `facility.py`, `reference.py`, `tests/unit/test_model_borrower.py`
**Behaviour:** every table and column from `plan.md §5.2`, with `borrower.reference` and `facility.reference` unique and never reused, `cin_fingerprint` unique among active borrowers, and `facility_conduct` unique on facility and date.
**Every case:** two active borrowers with the same CIN → unique violation on the fingerprint surfaced as `Conflict` offering the existing record; a facility whose `effective_from` precedes its predecessor's → `ValidationError` naming both rows; two conduct rows for the same facility and day → unique violation, which is what makes ingestion idempotent; a related party with no effective range → accepted, meaning currently effective, with the convention documented on the column.
**Steps:** 1. Write borrower, borrower group and the fingerprint column with its unique partial index. 2. Write related party and contact with encrypted name and identifier columns. 3. Write facility with effective dating and `superseded_by_id`. 4. Write `facility_conduct` with its unique constraint and date index. 5. Write the industry reference table and its seed shape.
**Tests:** `tests/unit/test_model_borrower.py` — `test_all_columns_match_plan`, `test_cin_fingerprint_unique_among_active`, `test_facility_effective_dating_rejects_overlap`, `test_conduct_unique_per_facility_day`, `test_references_are_stable_and_unique`.
**Run:** `pytest -q tests/unit/test_model_borrower.py` 5 passed.
**Done when:** the five tests pass and no plaintext CIN or PAN column exists in the schema.
**Evidence:** the test output, the generated DDL showing encrypted columns.

---

### [x] `T-009` · Model: covenant, test, signal, evidence, forecast, case, audit, trace, operations
`Milestone: M-0` · `Builds: R-01` · `Days: 1.5` · `Depends on: T-008` · `Snapshot: var/snapshots/T-009/` · `Build: DONE`

**Goal:** the remaining tables from `plan.md §5.3`–`§5.9`, including the covenant-version immutability trigger and the audit table that no code path may ever update or delete.
**Context:** three properties are declared here and defended for the life of the product: `covenant_version` is immutable once tested, `audit_event` is append-only with a hash chain, and `signal_event.content_hash` is unique so ingestion is idempotent for free. Each is enforced in the database *and* in code *and* in a test, because one enforcement point is a wish.
**Read first:** `plan.md §5.3` through `§5.9`, `src/covenant_radar/db/models/borrower.py`.
**Contracts:** the tables `C-30`–`C-41` and `C-60` operate on.
**Files owned:** `src/covenant_radar/db/models/document.py`, `covenant.py`, `signal.py`, `forecast.py`, `workflow.py`, `audit.py`, `operations.py`, `tests/unit/test_model_domain.py`
**Behaviour:** every table, column, type, nullability, constraint and index from `plan.md §5.3`–`§5.9`; a database trigger refusing `UPDATE` on `covenant_version` where `tested_at_least_once` is true for any column other than `status` and `effective_to`; `audit_event.sequence` monotonic with `prev_hash` and `hash`; `signal_event.content_hash` and `import_batch.content_hash` unique.
**Every case:** an update to a tested covenant version's threshold → refused by the trigger and surfaced as `Conflict` naming the rule; a second signal event with the same content hash → unique violation, caught by ingestion and reported as a duplicate rather than an error; a forecast row duplicating run, covenant version and horizon → unique violation; an audit row inserted with a `prev_hash` that does not match the previous row's `hash` → refused, so the chain cannot be started wrong.
**Steps:** 1. Write the document, page and span models. 2. Write covenant, covenant version, exception, waiver, test, schedule and ratio definition, and the immutability trigger for both engines. 3. Write signal event, evidence item, evidence transition and certificate request. 4. Write forecast run, forecast, path, driver, simulation, intervention and triage entry. 5. Write case, case event, comment, action, memo, export, override, disposition, notification and preference. 6. Write audit event, trace row, threshold snapshot, config version, model call, model registration, drift observation, job run, connector, feed source, entity match, retention purge log and evaluation run. 7. Add every index `plan.md §5` names.
**Tests:** `tests/unit/test_model_domain.py` — `test_all_tables_and_columns_match_plan`, `test_tested_covenant_version_update_refused`, `test_status_and_effective_to_still_updatable`, `test_signal_content_hash_unique`, `test_forecast_unique_per_run_covenant_horizon`, `test_audit_chain_rejects_wrong_prev_hash`, `test_no_update_or_delete_sql_against_audit_event_in_source` (scans the tree).
**Run:** `pytest -q tests/unit/test_model_domain.py` 7 passed.
**Done when:** the seven tests pass, the immutability trigger exists on both engines, and the source scan finds no update or delete against `audit_event`.
**Evidence:** the test output, the trigger DDL, the source-scan result.

---

### [x] `T-010` · Alembic setup, first migration, drift check, dual-engine support
`Milestone: M-0` · `Builds: R-01, R-02` · `Days: 1.5` · `Depends on: T-009` · `Snapshot: var/snapshots/T-010/` · `Build: DONE`

**Goal:** the schema is created and evolved only by versioned migrations, models and head can never silently disagree, and both engines are proven.
**Context:** `spec §R-01.b` and `R-01.c`. A schema created by `metadata.create_all` in production is a schema nobody can upgrade; `create_all` is permitted only in the unit-test fixture and a lint rule enforces it.
**Read first:** `plan.md §5` (the migration rules), `src/covenant_radar/db/models/`.
**Contracts:** `C-71` `radarctl migrate`.
**Files owned:** `alembic.ini`, `src/covenant_radar/db/migrations/env.py`, `script.py.mako`, `versions/0001_initial.py`, `src/covenant_radar/cli.py` (the `migrate` group), `tests/migration/test_initial.py`
**Behaviour:** `radarctl migrate upgrade` reaches head on an empty database of either engine; `downgrade` reverses to base; `check` compares models to head and fails on any difference; `current` prints the revision.
**Every case:** a model changed without a migration → `check` fails naming the difference and the gate fails with it; a migration failing part-way → the transaction rolls back whole and the recorded revision is unchanged; a migration run against a database at an unknown revision → refuse naming both revisions; an irreversible migration → declared as such with a stated reason, and `downgrade` refuses it explicitly rather than silently doing nothing.
**Steps:** 1. Configure Alembic for the `src` layout with both engines and the naming convention from `T-006`. 2. Autogenerate and hand-review the initial migration, including the immutability trigger and every index. 3. Implement the `migrate` command group. 4. Add the drift check to the gate. 5. Add a lint rule forbidding `create_all` outside the test fixture.
**Tests:** `tests/migration/test_initial.py` — `test_upgrade_creates_every_table_both_engines`, `test_downgrade_returns_to_base`, `test_check_detects_model_drift`, `test_failed_migration_rolls_back_and_keeps_revision`, `test_create_all_not_used_in_source`.
**Run:** `radarctl migrate upgrade` exit 0 on PostgreSQL and SQLite · `radarctl migrate check` exit 0 · `pytest -q tests/migration` 5 passed.
**Done when:** both engines upgrade and downgrade cleanly, drift is detected, and the gate includes the check.
**Evidence:** the migration log for both engines, the drift-check output.

---

### [x] `T-011` · Reference data loader and the seed command
`Milestone: M-0` · `Builds: R-01` · `Days: 1.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-011/` · `Build: DONE`

**Goal:** the reference data the product cannot run without — permissions, system roles, industry taxonomy, statement line definitions, ratio definitions, the action catalogue and the calendar — loads from versioned files, idempotently, and the same command creates a first administrator.
**Context:** reference data is versioned data, not fixtures. A change to a ratio definition's plausible band is a data change with a version, which is exactly why `spec §17.5` calls those bands configuration rather than literals.
**Read first:** `plan.md §5` (reference tables), `src/covenant_radar/db/models/`.
**Contracts:** `C-72` `radarctl seed`, `C-73` `radarctl user create`.
**Files owned:** `src/covenant_radar/db/seed/__init__.py`, `loader.py`, `data/permissions.json`, `data/roles.json`, `data/industries.json`, `data/statement_lines.json`, `data/calendar.json`, `src/covenant_radar/cli.py` (the `seed` and `user` groups), `tests/integration/test_seed.py`
**Behaviour:** `radarctl seed` loads every reference set idempotently — running twice changes nothing and reports so; `radarctl user create` creates a user with a forced password change and **never accepts a password as an argument**; each reference file carries a `taxonomy_version` and loading a newer version supersedes rather than overwrites.
**Every case:** a reference file with a duplicate code → refuse naming the code and load nothing; a reference row in use being removed by a newer version → mark it retired rather than delete, because history must still resolve; `--reset` on a database whose URL is not marked development → refuse without `--i-understand`; a password supplied on the command line → refuse and explain why.
**Steps:** 1. Write the seven system permissions sets and the seven system roles from `spec §16.1` as data. 2. Write the industry taxonomy, statement line definitions and calendar. 3. Write the idempotent loader with version supersession and retirement. 4. Implement `seed` and `user create`. 5. Add the reset guard.
**Tests:** `tests/integration/test_seed.py` — `test_seed_is_idempotent`, `test_roles_match_spec_matrix`, `test_newer_taxonomy_supersedes_not_overwrites`, `test_retired_reference_still_resolves`, `test_reset_refused_on_non_development_url`, `test_user_create_refuses_password_argument`.
**Run:** `radarctl seed` exit 0 twice with the second reporting no change · `radarctl user create --username admin --role administrator` exit 0 · `pytest -q tests/integration/test_seed.py` 6 passed.
**Done when:** the six tests pass and the seeded role permissions match `spec §16.1`'s matrix exactly.
**Evidence:** the double-run transcript, a matrix comparison output.

---

### [x] `T-012` · Threshold store: versioning, snapshot, approval, hot reload
`Milestone: M-0` · `Builds: N-03` · `Days: 1.5` · `Depends on: T-010` · `Snapshot: var/snapshots/T-012/` · `Build: DONE`

**Goal:** every number the product compares against lives in one versioned, approved, snapshotted store, and no threshold literal can exist in a branch anywhere in the source.
**Context:** `spec §17.5`'s T1–T12 with their boundary behaviour. Every record that decided anything names the snapshot in force, which is what makes a two-year-old warning reconstructable. `spec §16.1`: proposing and approving a threshold change are different permissions held by different roles.
**Read first:** `spec §17.5` (the table and every boundary), `plan.md §5.9` (`threshold_snapshot`), `src/covenant_radar/db/models/operations.py`.
**Contracts:** `C-18` propose and approve; `C-60` for the audit write.
**Files owned:** `src/covenant_radar/config/thresholds.py`, `config/thresholds.default.json`, `tests/unit/test_thresholds.py`, `tests/integration/test_threshold_approval.py`
**Behaviour:** `get(name)` returns the value in force from the active snapshot; `snapshot_id()` returns it for stamping onto records; `propose(values, actor)` creates a pending change; `approve(id, actor)` activates it, writes a new snapshot and an audit event carrying before and after; `reload()` re-reads the file and holds the last good values on failure.
**Every case:** an unknown threshold name → `KeyError` naming it and listing the valid names; a malformed file → the last good values stay in force and the error names the key and line; a value violating its own invariant, such as amber above act or a confidence floor above one → refuse the proposal naming the invariant; the proposer attempting to approve → `Conflict` naming the distinct-actor rule; no snapshot yet at first start → load the default file and record it as the first snapshot with source `default`.
**Steps:** 1. Write `config/thresholds.default.json` with T1–T12 exactly as `spec §17.5` states them. 2. Implement the store with the in-memory cache, `get`, `snapshot_id` and `reload`. 3. Implement propose and approve with the distinct-actor rule and the audit write. 4. Implement the invariant validators, one per threshold, each naming its rule. 5. Add a gate check that scans `src/` for numeric literals matching any threshold's value outside `thresholds.py` and the default file, and fails on a match.
**Tests:** `tests/unit/test_thresholds.py` — `test_twelve_thresholds_present_with_spec_values`, `test_unknown_name_lists_valid_names`, `test_malformed_file_keeps_last_good_and_names_line`, `test_amber_above_act_refused`, `test_no_threshold_literal_in_source`; `tests/integration/test_threshold_approval.py` — `test_proposal_pending_until_approved`, `test_proposer_cannot_approve`, `test_approval_writes_snapshot_and_audit_with_before_and_after`.
**Run:** `pytest -q tests/unit/test_thresholds.py tests/integration/test_threshold_approval.py` 8 passed · `python -c "from covenant_radar.config.thresholds import get; print(get('T1'))"` prints the act and amber values.
**Done when:** the eight tests pass and the literal scan finds no threshold value anywhere else in the source.
**Evidence:** the test output, the literal-scan result.

---

### [x] `T-013` · Local authentication: Argon2id, policy, lockout, sessions, MFA
`Milestone: M-0` · `Builds: N-04` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-013/` · `Build: DONE`

**Goal:** a local identity path that is safe by default, so a deployment without an identity provider is not a deployment with weak authentication.
**Context:** `spec §16.1`. Sessions are signed, HttpOnly, SameSite cookies with idle and absolute timeouts, invalidated on password change, role change and logout, and enumerable and revocable by an administrator. Authentication errors are generic and timing-safe so they do not reveal whether an account exists.
**Read first:** `spec §16.1`, `spec §16.3`, `plan.md §5.1` (`app_user`, `user_session`), `src/covenant_radar/core/errors.py`.
**Contracts:** the sign-in and sign-out routes; `C-60` for the audit writes.
**Files owned:** `src/covenant_radar/security/passwords.py`, `sessions.py`, `mfa.py`, `src/covenant_radar/services/auth.py`, `src/covenant_radar/web/routes/auth.py`, `src/covenant_radar/web/templates/screens/auth/*`, `tests/unit/test_passwords.py`, `tests/integration/test_auth_local.py`
**Behaviour:** Argon2id hashing with configured parameters and transparent rehash on parameter change; a configurable policy for length, complexity and reuse; lockout after a configured count with an unlock window; TOTP second factor when enabled; session issue, refresh, idle and absolute expiry, and revocation; every authentication outcome audited.
**Every case:** a wrong password → generic message, constant-time comparison, failure counted, audited; a locked account → the same generic message, so lockout is not an oracle, with the real reason only in the audit trail; a password change → every other session for that user revoked; a role change → the user's sessions revoked so the new permissions take effect immediately; an expired session → redirect to sign-in preserving the intended destination and any unsaved form input; MFA enabled but not enrolled → forced enrolment before any other screen; a first sign-in → forced password change.
**Steps:** 1. Implement hashing, verification, rehash-on-verify and the policy. 2. Implement lockout with the window and the audit events. 3. Implement session issue, validation, refresh, revocation and the cookie attributes. 4. Implement TOTP enrolment and verification. 5. Write the sign-in, sign-out, password-change and MFA-enrolment routes and screens. 6. Audit every outcome.
**Tests:** `tests/unit/test_passwords.py` — `test_argon2id_parameters`, `test_rehash_on_parameter_change`, `test_policy_rejects_weak`, `test_constant_time_comparison`; `tests/integration/test_auth_local.py` — `test_wrong_password_generic_message`, `test_locked_account_same_message`, `test_password_change_revokes_other_sessions`, `test_role_change_revokes_sessions`, `test_session_idle_and_absolute_expiry`, `test_expired_session_preserves_destination`, `test_mfa_enrolment_forced_when_enabled`, `test_every_outcome_audited`.
**Run:** `pytest -q tests/unit/test_passwords.py tests/integration/test_auth_local.py` 12 passed.
**Done when:** the twelve tests pass and no authentication path reveals whether an account exists.
**Evidence:** the test output, a sample session cookie's attributes.

---

### [x] `T-014` · OIDC and SAML authentication providers
`Milestone: M-0` · `Builds: N-04` · `Days: 2.0` · `Depends on: T-013` · `Snapshot: var/snapshots/T-014/` · `Build: DONE`

**Goal:** the bank's own identity provider is the authentication path where one exists, configured rather than coded, with the local store remaining available for break-glass access.
**Context:** `spec §16.1`; `spec §12.1`'s [OPEN-03] means test metadata may not exist yet, so both flows are developed and tested against a local provider fixture and configured at deployment.
**Read first:** `src/covenant_radar/security/sessions.py`, `src/covenant_radar/config/settings.py`, `src/covenant_radar/services/auth.py`.
**Contracts:** the authentication routes; `C-60`.
**Files owned:** `src/covenant_radar/security/oidc.py`, `saml.py`, `provisioning.py`, `src/covenant_radar/web/routes/auth.py` (the SSO routes), `tests/integration/test_auth_sso.py`, `tests/fixtures/idp/*`
**Behaviour:** OIDC authorization-code flow with PKCE, state and nonce validation, discovery, and JWKS caching with rotation; SAML 2.0 with signed assertions, audience and recipient validation, replay protection and clock-skew tolerance; attribute mapping to user, roles and portfolio scope, configurable per deployment; just-in-time provisioning with a configurable default role that is never an administrative one.
**Every case:** an unsigned or wrongly signed assertion → refuse, audit a security event, generic message to the user; a replayed assertion → refuse on the replay cache; a mismatched audience, recipient, state or nonce → refuse naming which in the audit event only; an expired assertion beyond skew tolerance → refuse; an attribute mapping producing an unknown role → provision with the default role and raise an administrator notification, never guess; the provider unreachable → local sign-in remains available and the screen says which path is unavailable.
**Steps:** 1. Implement the OIDC client with discovery, PKCE and JWKS rotation. 2. Implement the SAML service provider with signature, audience, recipient, expiry and replay checks. 3. Implement attribute mapping and just-in-time provisioning. 4. Wire both into the session issuance from `T-013`. 5. Build a local identity-provider fixture so the suite runs offline. 6. Audit every outcome.
**Tests:** `tests/integration/test_auth_sso.py` — `test_oidc_happy_path_issues_session`, `test_oidc_state_mismatch_refused`, `test_oidc_nonce_mismatch_refused`, `test_jwks_rotation_handled`, `test_saml_unsigned_assertion_refused`, `test_saml_replay_refused`, `test_saml_audience_mismatch_refused`, `test_attribute_mapping_to_roles_and_scope`, `test_unknown_role_provisions_default_and_notifies`, `test_provider_down_leaves_local_path_available`.
**Run:** `pytest -q tests/integration/test_auth_sso.py` 10 passed.
**Done when:** the ten tests pass against the local provider fixture with no network access.
**Evidence:** the test output, the attribute-mapping configuration example.

---

### [x] `T-015` · Permission model and declarative enforcement
`Milestone: M-0` · `Builds: N-04` · `Days: 2.0` · `Depends on: T-013` · `Snapshot: var/snapshots/T-015/` · `Build: DONE`

**Goal:** authorization is a declaration on the route, checked before the handler runs, and a route that forgets to declare one cannot be registered.
**Context:** `spec §16.1`'s matrix. Two rows carry "no role, ever, in any configuration": confirming a covenant that failed verification, and approving a credit action inside the tool. Those are structural — the first has no code path and the second has no endpoint — and this task establishes the mechanism that makes that statement checkable.
**Read first:** `spec §16.1` (the full matrix), `src/covenant_radar/db/models/identity.py`, `plan.md §6`'s standing identity answer.
**Contracts:** the permission enumeration every `C-01`…`C-22` row names.
**Files owned:** `src/covenant_radar/security/permissions.py`, `rbac.py`, `src/covenant_radar/api/deps.py`, `tests/unit/test_permissions.py`, `tests/security/test_route_declarations.py`
**Behaviour:** an enumerated `Permission` with one member per matrix row; a `requires(Permission, subject=...)` dependency resolving the principal and refusing with `403` naming the missing permission before the handler runs; a registry check at application startup that every registered route declares either a permission or an explicit public marker.
**Every case:** a route registered with no declaration → the application refuses to start naming the route, so the omission cannot reach production; a principal without the permission → `403` naming it, and the audit event records the refusal; an API key without the scope → `403`; a permission that exists in the enumeration but is granted to no role → the startup check reports it, because an unreachable permission is either a defect or dead code; a public route → explicitly marked and listed in the startup log.
**Steps:** 1. Define `Permission` with one member per matrix row and a docstring naming the matrix cell. 2. Implement role-permission resolution with caching invalidated on role change. 3. Implement the `requires` dependency for both the web and API surfaces. 4. Implement the startup registry check. 5. Write the security test that enumerates every route and asserts a declaration.
**Tests:** `tests/unit/test_permissions.py` — `test_enumeration_matches_spec_matrix`, `test_role_resolution_cached_and_invalidated`, `test_unreachable_permission_reported`; `tests/security/test_route_declarations.py` — `test_every_route_declares_permission_or_public`, `test_missing_declaration_refuses_startup`, `test_refusal_names_missing_permission`, `test_refusal_is_audited`.
**Run:** `pytest -q tests/unit/test_permissions.py tests/security/test_route_declarations.py` 7 passed.
**Done when:** the seven tests pass and an undeclared route prevents startup.
**Evidence:** the test output, the startup declaration listing.

---

### [x] `T-016` · Row-level portfolio scoping in the repository layer
`Milestone: M-0` · `Builds: N-04` · `Days: 1.5` · `Depends on: T-015` · `Snapshot: var/snapshots/T-016/` · `Build: DONE`

**Goal:** a user cannot read another portfolio's data through any surface, because the predicate is applied in the query and not in the template.
**Context:** `spec §R-02.b` and `N-04.c`. A scoped read returns `404`, not `403`, so the scope is not an enumeration oracle. The materialised path from `T-007` makes the predicate one indexed prefix match.
**Read first:** `plan.md §5.1` (`user_portfolio_scope`, `portfolio.path`), `src/covenant_radar/db/base.py`, `ports/repository.py`.
**Contracts:** `C-58` `Repository[T]` with scope-carrying reads.
**Files owned:** `src/covenant_radar/db/scoping.py`, `src/covenant_radar/db/repositories/base.py`, `tests/unit/test_scoping.py`, `tests/security/test_scope_leakage.py`
**Behaviour:** a `Scope` value derived once per request from the principal; every repository read composing the scope predicate into its statement; a helper that resolves an entity to its owning portfolio path for entities that reach it through a join; and an unscoped-read escape hatch that is explicitly named, audited, and usable only by the auditor role and the retention job.
**Every case:** a read with an empty scope → returns nothing, never everything, because absence of scope is absence of access; an entity reachable only through a join → the predicate follows the join and a test proves it per entity; the explicit unscoped read → permitted only for the two named callers, audited every time, and a test asserts no other caller uses it; a scope covering a parent portfolio with descendants included → matches by path prefix; a request for an out-of-scope entity by direct id → `404`.
**Steps:** 1. Implement `Scope` resolution from the principal, with descendant inclusion. 2. Implement predicate composition in the repository base for direct and joined ownership. 3. Implement the named unscoped read with its audit write and caller allow-list. 4. Write the leakage test that, for every repository, seeds two portfolios and asserts a user of one cannot reach the other's rows by list, filter, search or direct id.
**Tests:** `tests/unit/test_scoping.py` — `test_empty_scope_returns_nothing`, `test_descendants_matched_by_path_prefix`, `test_unscoped_read_restricted_and_audited`; `tests/security/test_scope_leakage.py` — `test_no_repository_leaks_across_portfolios`, `test_direct_id_returns_404_not_403`, `test_joined_entities_follow_the_predicate`.
**Run:** `pytest -q tests/unit/test_scoping.py tests/security/test_scope_leakage.py` 6 passed.
**Done when:** the six tests pass, the leakage test covers every repository that exists, and the test is written so a new repository without scoping fails it.
**Evidence:** the leakage-test output listing every repository covered.

---

### [x] `T-017` · Field-level encryption, key handling and secret loading
`Milestone: M-0` · `Builds: N-04` · `Days: 1.5` · `Depends on: T-006` · `Snapshot: var/snapshots/T-017/` · `Build: DONE`

**Goal:** personal-class columns are unreadable in a raw database dump, the key lives outside the database, and rotation is a documented procedure rather than a migration nobody dares run.
**Context:** `spec §16.2` and `N-04.f`. The deterministic fingerprint for CIN is an HMAC with a separate key so uniqueness and lookup work without decryption and the column is not a rainbow-table target.
**Read first:** `spec §16.2`, `plan.md §5.2` (the encrypted columns), `src/covenant_radar/db/types.py`.
**Contracts:** none; the type `T-008`'s columns already declare.
**Files owned:** `src/covenant_radar/security/crypto.py`, `secrets.py`, `src/covenant_radar/db/types.py` (the encrypted and fingerprint types), `docs/adr/0002-field-encryption-and-rotation.md`, `tests/unit/test_crypto.py`, `tests/integration/test_encrypted_columns.py`
**Behaviour:** authenticated encryption with a key identifier stored alongside the ciphertext so rotation can proceed row by row; an HMAC fingerprint type for deterministic lookup; keys loaded from the environment or the OS keyring only; a rotation command that re-encrypts in batches with progress and is resumable.
**Every case:** a missing key at startup → refuse to start naming the variable, never fall back to plaintext; ciphertext whose key identifier is unknown → `ExternalServiceError` naming the identifier, never a silent null; a rotation interrupted → resumable from the last committed batch, with no row left half-rotated; a raw dump inspected → no plaintext personal-class value present, asserted by a test; a fingerprint computed twice for the same input → identical, and for different inputs → different.
**Steps:** 1. Implement the encryption service with key identifiers and authenticated encryption. 2. Implement the fingerprint type with its separate key. 3. Implement secret loading from environment and keyring with the no-file rule from `T-004`. 4. Implement the resumable rotation command. 5. Write `ADR-0002` covering the scheme, the rotation procedure and what it does not protect against.
**Tests:** `tests/unit/test_crypto.py` — `test_round_trip`, `test_ciphertext_carries_key_id`, `test_unknown_key_id_raises`, `test_fingerprint_deterministic_and_distinct`, `test_missing_key_refuses_start`; `tests/integration/test_encrypted_columns.py` — `test_raw_dump_contains_no_plaintext_personal_value`, `test_rotation_is_resumable`.
**Run:** `pytest -q tests/unit/test_crypto.py tests/integration/test_encrypted_columns.py` 7 passed.
**Done when:** the seven tests pass and a raw dump of a seeded database contains no plaintext personal-class value.
**Evidence:** the dump-scan output, `ADR-0002`.

---

### [x] `T-018` · Maker-checker framework
`Milestone: M-0` · `Builds: N-04` · `Days: 1.0` · `Depends on: T-015` · `Snapshot: var/snapshots/T-018/` · `Build: DONE`

**Goal:** one mechanism for every operation that needs a second pair of eyes, so covenant registration, threshold change, catalogue change and model promotion all behave identically and none reinvents it.
**Context:** `spec §16.1`. The distinct-actor constraint is already in the database from `T-007`; this task is the service and the workflow around it.
**Read first:** `plan.md §5.1` (`maker_checker_request`), `src/covenant_radar/security/permissions.py`.
**Contracts:** `C-07`, `C-18`; `C-60` for the audit writes.
**Files owned:** `src/covenant_radar/security/maker_checker.py`, `src/covenant_radar/services/approvals.py`, `tests/unit/test_maker_checker.py`, `tests/integration/test_approval_flow.py`
**Behaviour:** `submit(operation, subject, payload, maker)` creates a pending request and returns it; `decide(request_id, checker, approved, reason)` applies or rejects it, writing an audit event either way; a generic pending-approvals list scoped to the checker's permissions; and expiry after a configured window with notification.
**Every case:** maker equals checker → `Conflict` naming the rule, refused before the database constraint is reached and again by it; a request decided twice → `Conflict` naming the prior decision; a rejection with no reason → `ValidationError`; an expired request → cannot be approved, and the state says expired rather than pending; maker-checker disabled by configuration for an operation → the operation applies directly and the audit event records that no second actor was required, so the absence is visible rather than invisible.
**Steps:** 1. Implement submit, decide, list and expire. 2. Implement the payload application callback registry, one per operation type. 3. Wire the permission checks so proposing and approving are distinct permissions. 4. Audit submission, approval, rejection and expiry. 5. Make the disabled case explicit in the audit payload.
**Tests:** `tests/unit/test_maker_checker.py` — `test_maker_cannot_check`, `test_double_decision_refused`, `test_rejection_requires_reason`, `test_expiry_blocks_approval`; `tests/integration/test_approval_flow.py` — `test_pending_list_scoped_to_checker`, `test_approval_applies_payload_and_audits`, `test_disabled_mode_records_absence_of_checker`.
**Run:** `pytest -q tests/unit/test_maker_checker.py tests/integration/test_approval_flow.py` 7 passed.
**Done when:** the seven tests pass and every operation type registers exactly one application callback.
**Evidence:** the test output, the registered operation list.

---

### [x] `T-019` · Security headers, CSP, CSRF, rate limiting, upload guard
`Milestone: M-0` · `Builds: N-04` · `Days: 1.5` · `Depends on: T-015` · `Snapshot: var/snapshots/T-019/` · `Build: DONE`

**Goal:** the application-level hardening `spec §16.3` requires, present from the first screen rather than added after a penetration test finds its absence.
**Context:** the content security policy permits **no external origin**, which is what makes `spec §11.1`'s no-CDN rule enforceable rather than aspirational: a violation becomes an error, not a silent degradation.
**Read first:** `spec §16.3`, `src/covenant_radar/config/settings.py`.
**Contracts:** the standing rules in `plan.md §6`.
**Files owned:** `src/covenant_radar/security/headers.py`, `ratelimit.py`, `uploads.py`, `csrf.py`, `SECURITY.md`, `tests/security/test_hardening.py`
**Behaviour:** middleware setting CSP with no external origin, HSTS, `X-Content-Type-Options`, `Referrer-Policy` and `frame-ancestors`; CSRF tokens on every state-changing form with rotation on privilege change; configurable rate limits on authentication, password reset and the API returning `429` with `Retry-After`; upload validation of extension, declared type, magic bytes and size before the file reaches any store, with a virus-scan hook.
**Every case:** a template referencing an external origin → the CSP report-only test fails the build, so it is caught in development rather than in production; a form posted without a token → `403` with a message that does not leak the token; a token from a prior session → refused; a rate limit reached → `429` with `Retry-After`, and the audit records it; an upload whose magic bytes disagree with its declared type → refused naming both; an upload above the limit → refused with the limit stated, and nothing is written.
**Steps:** 1. Implement the headers middleware, driven by settings. 2. Implement CSRF with per-session tokens and rotation. 3. Implement rate limiting with a pluggable store defaulting to in-process. 4. Implement upload validation with the scan hook. 5. Write `SECURITY.md` with the reporting process and the supported-version policy. 6. Add a test that renders every template and fails on any external origin.
**Tests:** `tests/security/test_hardening.py` — `test_csp_has_no_external_origin`, `test_no_template_references_external_origin`, `test_all_headers_present`, `test_csrf_required_on_state_change`, `test_stale_csrf_token_refused`, `test_rate_limit_returns_429_with_retry_after`, `test_upload_magic_byte_mismatch_refused`, `test_oversize_upload_refused_and_not_stored`.
**Run:** `pytest -q tests/security/test_hardening.py` 8 passed.
**Done when:** the eight tests pass and no template references any external origin.
**Evidence:** the test output, the emitted header set.

---

### [x] `T-020` · Design tokens, both themes, fonts, contrast check
`Milestone: M-0` · `Supports: plan §7.2` · `Days: 1.5` · `Depends on: T-001` · `Snapshot: var/snapshots/T-020/` · `Build: DONE`

**Goal:** every design value lives in one file, both themes meet their contrast floors, the typefaces are self-hosted, and a literal design value anywhere else fails the build.
**Context:** `plan.md §7.2` gives the complete token file including the dark palette derived by role rather than inverted. `spec §15.6`: 7:1 for text and 4.5:1 for accent chips, **in both themes**. Font licensing is `plan.md [OPEN-15]`; take its default — metric-compatible open families with recorded licences — and record the choice.
**Read first:** `plan.md §7.2` (the token file), `spec §15.3`, `spec §15.6`.
**Contracts:** none.
**Files owned:** `src/covenant_radar/web/static/css/tokens.css`, `src/covenant_radar/web/static/fonts/*`, `fonts/LICENSES.md`, `scripts/check_contrast.py`, `docs/adr/0003-typography-and-theming.md`, `tests/unit/test_tokens.py`
**Behaviour:** `tokens.css` declares exactly the custom properties `plan.md §7.2` lists and nothing else — no selector beyond `:root`, `[data-theme="dark"]` and the reduced-motion query. `scripts/check_contrast.py` computes the WCAG ratio for every foreground-on-background pair in use in both themes and fails below the floors.
**Every case:** a pair below its floor → the script fails naming the pair, the ratio and the required floor; a hex, px, ms or easing literal in any other stylesheet or template → the gate check fails naming the file and line; a font file missing its licence entry → the check fails; the ₹ glyph or a Devanagari codepoint missing from a stack → the rendering test fails, and the fallback supplying it is a pass rather than a defect.
**Steps:** 1. Write `tokens.css` exactly as `plan.md §7.2` specifies. 2. Vendor the four families with subsetting, and record every licence. 3. Write the contrast checker over the declared pairs in both themes. 4. Write the literal scanner and add both to the gate. 5. Write a glyph-coverage test for ₹ and Devanagari at the smallest and largest sizes in use. 6. Record `ADR-0003`.
**Tests:** `tests/unit/test_tokens.py` — `test_every_token_from_plan_present`, `test_dark_theme_redefines_every_colour_role`, `test_no_selector_beyond_root_theme_and_reduced_motion`, `test_reduced_motion_zeroes_durations`, `test_no_design_literal_outside_tokens`, `test_rupee_and_devanagari_covered`, `test_every_font_has_a_licence_entry`.
**Run:** `pytest -q tests/unit/test_tokens.py` 7 passed · `python scripts/check_contrast.py` prints every pair with its ratio and exits 0.
**Done when:** the seven tests pass, both themes clear their floors, and no design literal exists outside the token file.
**Evidence:** the contrast report for both themes, `ADR-0003`.

---

### [x] `T-021` · The eighteen components and state partials
`Milestone: M-0` · `Supports: plan §7.3` · `Days: 2.0` · `Depends on: T-020` · `Snapshot: var/snapshots/T-021/` · `Build: DONE`

**Goal:** every reusable part exists with its named states before any screen is built, because a screen built before its components is a screen rebuilt after them.
**Context:** `plan.md §7.3`'s inventory of eighteen, and `spec §15.2`'s refusals, which bind every one of them. These are hand-written macros, which is why there is no component library to leave at its defaults.
**Read first:** `plan.md §7.3` (the inventory and states), `spec §15.2` (the refusals), `src/covenant_radar/web/static/css/tokens.css`.
**Contracts:** none; components are internal.
**Files owned:** `src/covenant_radar/web/templates/_components/*.html`, `_states/*.html`, `src/covenant_radar/web/static/css/app.css`, `src/covenant_radar/web/static/js/app.js`, `src/covenant_radar/web/static/vendor/htmx/*`, `tests/unit/test_components.py`, `tests/e2e/test_component_gallery.py`
**Behaviour:** eighteen macros, each taking explicit parameters and no globals, each rendering valid accessible HTML, each styled only through `var(--…)`. A gallery page renders every component in every state and is what the visual review and the accessibility suite run against.
**Every case:** a `band_chip` given a band outside act, amber and watch → renders the neutral chip with no accent, because the accent may only mean risk; a `ledger_table` with zero rows → renders the empty state, never an empty body; a long Indian entity name → wraps rather than truncating with an ellipsis, because the layout is designed for real lengths; a `trajectory` asked to render without its ledger figures → refuses, because `spec §15.2`'s third forbid is a build rule and not advice; JavaScript disabled → every component still renders and every control still works except the horizon control, which is `T-077`'s concern.
**Steps:** 1. Vendor HTMX with its licence and integrity hash. 2. Write the eighteen macros with their states. 3. Write `app.css` using only tokens. 4. Write `app.js` with progressive enhancement only — drawer open and close, toast dismissal, filter submission. 5. Build the gallery page. 6. Give every focusable element the token focus ring and a 32px minimum target.
**Tests:** `tests/unit/test_components.py` — `test_eighteen_macros_defined`, `test_no_design_literal_in_app_css`, `test_band_chip_rejects_unknown_band`, `test_empty_table_renders_empty_state`, `test_trajectory_requires_ledger_figures`, `test_long_name_wraps_not_truncates`, `test_every_interactive_target_at_least_32px`, `test_only_drawer_casts_a_shadow`; `tests/e2e/test_component_gallery.py` — `test_gallery_renders_every_state_both_themes`, `test_gallery_passes_axe_both_themes`.
**Run:** `pytest -q tests/unit/test_components.py` 8 passed · `pytest -q tests/e2e/test_component_gallery.py` 2 passed · gallery screenshots written for both themes at three viewports.
**Done when:** the ten tests pass, the gallery shows every component in every state in both themes, and a human has reviewed it against `spec §15.2`'s refusal list item by item.
**Evidence:** the gallery screenshots, the reviewer's sign-off note in `MERGE_LOG.md`.

---

### [x] `T-022` · Application shell: ASGI factory, routing, i18n scaffold, 404 and 500
`Milestone: M-0` · `Supports: plan §7.4` · `Days: 1.5` · `Depends on: T-021, T-015` · `Snapshot: var/snapshots/T-022/` · `Build: DONE`

**Goal:** the application starts, every request carries an identity and a request id, unknown routes get a designed page rather than a stack trace, and every user-facing string is already resolving from a catalogue before the first screen is written.
**Context:** retrofitting internationalisation after thirty screens is thirty screens rewritten, so `R-35`'s catalogue mechanism starts here even though the Hindi translation lands at `T-141`.
**Read first:** `src/covenant_radar/security/rbac.py`, `core/context.py`, `observability/logging.py`, `web/templates/_components/`.
**Contracts:** `C-70` `radarctl serve`; `C-23` health, readiness and version.
**Files owned:** `src/covenant_radar/asgi.py`, `src/covenant_radar/web/__init__.py`, `web/middleware.py`, `web/errors.py`, `web/templates/base.html`, `web/templates/screens/_404.html`, `_500.html`, `src/covenant_radar/i18n/__init__.py`, `formatting.py`, `src/covenant_radar/cli.py` (the `serve` command), `tests/integration/test_shell.py`
**Behaviour:** an application factory wiring settings, middleware, routers, templates, static files and error handlers; middleware minting the request id, resolving the principal, starting the trace span and writing one log line per request; error handlers mapping the exception hierarchy to statuses and to designed pages; a translation function available in every template with a build check that fails on a literal user-facing string; Indian number, currency, date and quarter formatting.
**Every case:** an unknown route → the designed 404, never a framework page; an unhandled exception → the designed 500 with a support reference, one log line at error level with the class name, and never a traceback in the response; a literal user-facing string in a template → the build check fails naming the file and line; a request with no session → anonymous principal and a redirect to sign-in for protected routes; the configured port busy → exit 3 naming the port.
**Steps:** 1. Write the factory and the middleware stack in the correct order. 2. Write the error handlers and both designed pages. 3. Write `base.html` with one `h1` block, a skip link, the theme attribute resolved server-side to avoid a flash, the language attribute and the navigation. 4. Write the i18n scaffold with catalogue loading, the template function and the literal-string build check. 5. Write Indian formatting for lakh and crore, IST dates and FY quarters. 6. Implement `radarctl serve`.
**Tests:** `tests/integration/test_shell.py` — `test_health_and_version`, `test_request_id_on_every_log_line`, `test_unknown_route_designed_404`, `test_exception_renders_500_without_traceback`, `test_literal_string_fails_build_check`, `test_theme_resolved_server_side_no_flash`, `test_lakh_crore_formatting`, `test_fy_quarter_formatting`, `test_port_busy_exits_3`.
**Run:** `pytest -q tests/integration/test_shell.py` 9 passed · `radarctl serve` then `curl -s localhost:8000/health` returns healthy JSON.
**Done when:** the nine tests pass and no user-facing literal string exists in any template.
**Evidence:** the test output, the 404 and 500 screenshots in both themes.

---

### [x] `T-023` · Master data: services, screens and API for borrower, facility, portfolio
`Milestone: M-0` · `Builds: R-02` · `Days: 1.5` · `Depends on: T-022, T-016` · `Snapshot: var/snapshots/T-023/` · `Build: DONE`

**Goal:** the first vertical slice through every layer — a scoped user creates, reads, edits and deactivates the entities everything else attaches to — proving that the foundation actually composes.
**Context:** `spec §R-02`. Effective dating means an edit to a facility limit inserts a new row and closes the old, so a covenant test dated before the change uses the prior limit. This is the first time services, repositories, scoping, RBAC, audit, templates and the API are exercised together, and it is deliberately early for that reason.
**Read first:** `plan.md §5.2`, `src/covenant_radar/db/repositories/base.py`, `security/rbac.py`, `web/templates/_components/`.
**Contracts:** `C-21` for the read resources; `C-60` for the audit writes.
**Files owned:** `src/covenant_radar/services/master_data.py`, `src/covenant_radar/db/repositories/borrower.py`, `facility.py`, `portfolio.py`, `src/covenant_radar/web/routes/master_data.py`, `web/templates/screens/master_data/*`, `src/covenant_radar/api/v1/routers/borrowers.py`, `facilities.py`, `api/v1/schemas/master_data.py`, `tests/integration/test_master_data.py`
**Behaviour:** list, detail, create, edit and deactivate for borrower, facility and portfolio, through both the web screens and the API, with scope applied in the query, permissions declared on the route, and every change audited.
**Every case:** a limit change → a new effective-dated row with the old closed, and a test proving a date-scoped read returns the prior value; deactivating a borrower with live facilities → refused naming them; a duplicate CIN → refused, with the existing record offered; a scoped user requesting an out-of-scope borrower → `404`; an edit with a stale version → `Conflict` naming what changed and who changed it; a create with a missing required field → `422` with the field path.
**Steps:** 1. Write the three repositories with scope-carrying reads. 2. Write the service with effective-dated updates, the deactivation guard and the duplicate check. 3. Write the web routes and screens using only components. 4. Write the API routers and schemas. 5. Emit audit events for every change.
**Tests:** `tests/integration/test_master_data.py` — `test_limit_change_creates_effective_dated_row`, `test_dated_read_returns_prior_limit`, `test_deactivate_with_live_facilities_refused`, `test_duplicate_cin_refused_offers_existing`, `test_out_of_scope_returns_404`, `test_stale_version_conflict_names_change`, `test_missing_field_422_with_path`, `test_every_change_audited`, `test_web_and_api_agree`.
**Run:** `pytest -q tests/integration/test_master_data.py` 9 passed · screenshots for the three screens at three viewports in both themes.
**Done when:** the nine tests pass, the web and API surfaces return the same data for the same principal, and M-0's gate can be demonstrated.
**Evidence:** the test output, the screenshots, the M-0 gate record.

---

### M-1 · Covenant core — 29.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-024` · Statement chart of accounts and the normalisation model
`Milestone: M-1` · `Builds: R-03` · `Days: 1.5` · `Depends on: T-010` · `Snapshot: var/snapshots/T-024/` · `Build: #1 · Phase 1 · cum 0.4h`

**Goal:** one normalised chart of statement lines that every ratio reads and every import maps onto, so a ratio never depends on a customer's column names.
**Context:** `plan.md §5.3`. Sign conventions are the trap: Indian extracts variously present liabilities positive, expenses negative and both. The convention is declared per line definition and applied at normalisation, once.
**Read first:** `plan.md §5.3`, `src/covenant_radar/db/models/borrower.py`, `db/seed/data/statement_lines.json`.
**Contracts:** the `lines` mapping `C-30` consumes.
**Files owned:** `src/covenant_radar/domain/statements/__init__.py`, `chart.py`, `identities.py`, `src/covenant_radar/db/seed/data/statement_lines.json`, `tests/unit/test_chart.py`
**Behaviour:** the chart declares every line with its statement, sign convention, whether it is derived and its derivation; `normalise(raw, mapping)` returns a validated line mapping in ₹ crore; balance-sheet and profit-and-loss identities are checked within a configured tolerance.
**Every case:** a derived line supplied directly and also derivable → the supplied value wins and the discrepancy beyond tolerance is reported, never silently reconciled; an identity failing beyond tolerance → the period is marked incomplete with the failing identity named, and ratios that need it return not-computable; a line absent → absent, never zero; a negative value on a line whose convention forbids it → flagged for review rather than sign-flipped, because guessing a sign is how a ratio becomes wrong quietly.
**Steps:** 1. Define the line definitions covering everything the 24 ratios need. 2. Implement `normalise` with unit and sign handling. 3. Implement the identity checks with configured tolerance. 4. Implement derived-line computation and the discrepancy report. 5. Seed the definitions as versioned reference data.
**Tests:** `tests/unit/test_chart.py` — `test_every_ratio_input_line_defined`, `test_normalise_to_crore`, `test_sign_convention_applied`, `test_identity_failure_marks_incomplete_and_names_it`, `test_absent_line_is_absent_not_zero`, `test_supplied_beats_derived_and_reports_discrepancy`.
**Run:** `pytest -q tests/unit/test_chart.py` 6 passed.
**Done when:** the six tests pass and every line the ratio library needs exists in the chart.
**Evidence:** the chart listing cross-referenced against the ratio inputs.

---

### T-025 · Statement import: CSV, XLSX, JSON and API, with mapping and validation
`Milestone: M-1` · `Builds: R-03` · `Days: 2.5` · `Depends on: T-024` · `Snapshot: var/snapshots/T-025/` · `Build: COMPLETED`

**Goal:** a bank's actual extract loads, reconciles and reports, with bad rows quarantined rather than dropped and the whole batch idempotent.
**Context:** `spec §R-03`. Import is where customer data quality meets the product (`spec` RISK-04), so the report and the quarantine are the deliverable as much as the loaded rows.
**Read first:** `plan.md §5.3` (`import_batch`, `quarantine_row`, `import_mapping`), `src/covenant_radar/domain/statements/chart.py`.
**Contracts:** `C-22` `POST /api/v1/ingest/statements`.
**Files owned:** `src/covenant_radar/ingestion/statements/__init__.py`, `mapping.py`, `readers.py`, `normalise.py`, `validate.py`, `src/covenant_radar/services/statements.py`, `src/covenant_radar/api/v1/routers/ingest.py` (statements only), `tests/integration/test_statement_import.py`, `tests/fixtures/statements/*`
**Behaviour:** a versioned mapping per source describing columns, units, currency, sign and the borrower key; readers for CSV, XLSX and JSON plus the API payload; row-level validation; a batch record with counts and a reconciliation report; and idempotence on the batch content hash.
**Every case:** the same file imported twice → the second returns the first batch's result and creates nothing; one bad row → quarantined with the failing rule named, every other row loaded, and the report naming the quarantine; a file whose columns do not match the mapping → refused before any row is written, naming the difference; a unit or currency stated in the mapping → converted with the conversion recorded in provenance; a borrower key that matches no borrower → quarantined, never auto-created; a totals row present in the extract → recognised by the mapping and used for reconciliation, not loaded as data.
**Steps:** 1. Implement the mapping model and its validation. 2. Implement the three readers behind one interface. 3. Implement row normalisation through `T-024`'s chart. 4. Implement validation, quarantine and the reconciliation report. 5. Implement the batch with its content hash and idempotence. 6. Expose the API ingest route.
**Tests:** `tests/integration/test_statement_import.py` — `test_clean_extract_loads_and_reconciles`, `test_reimport_is_idempotent`, `test_bad_row_quarantined_rest_load`, `test_column_mismatch_refused_before_write`, `test_unit_and_currency_conversion_recorded`, `test_unknown_borrower_key_quarantined`, `test_totals_row_used_for_reconciliation_not_loaded`, `test_report_counts_match_actuals`.
**Run:** `pytest -q tests/integration/test_statement_import.py` 8 passed · `radarctl` import of the fixture extract prints the reconciliation report.
**Done when:** the eight tests pass and the report's counts reconcile against the source control totals.
**Evidence:** a reconciliation report, a quarantine listing.

---

### T-026 · Provenance, restatement and quarantine resolution
`Milestone: M-1` · `Builds: R-03` · `Days: 1.5` · `Depends on: T-025` · `Snapshot: var/snapshots/T-026/` · `Build: COMPLETED`

**Goal:** every stored figure can name the file, row and mapping version it came from, a corrected prior period supersedes without deleting, and a quarantined row has a way back in.
**Context:** `spec §R-03.b` and `R-20.a`. Restatement is not an edit: a corrected quarter creates a new period version, marks dependent covenant tests for recomputation, and keeps the old.
**Read first:** `plan.md §5.3`, `src/covenant_radar/ingestion/statements/`.
**Contracts:** `C-60` for the audit writes.
**Files owned:** `src/covenant_radar/ingestion/statements/restate.py`, `provenance.py`, `src/covenant_radar/services/statements.py` (restatement and quarantine paths), `src/covenant_radar/web/routes/statements.py`, `web/templates/screens/statements/*`, `tests/integration/test_restatement.py`
**Behaviour:** provenance written per field at import; a restatement creating a new period version with `superseded_by_id` set on the old; dependent covenant tests flagged for recomputation; a quarantine review screen where a steward corrects and re-submits a row with a reason, which is itself a new provenance record and an audit event.
**Every case:** a restatement of a period with no dependent tests → succeeds and flags nothing; with dependent tests → every one flagged, listed to the steward, and none silently recomputed without the batch that does it being recorded; a quarantined row corrected → loaded with provenance naming both the original and the correction; a quarantined row rejected → closed with a reason and retained for the configured window; a provenance query for any stored value → returns source, row and mapping version.
**Steps:** 1. Implement provenance writing at every import path. 2. Implement restatement with versioning and dependent flagging. 3. Implement quarantine review, correction and rejection. 4. Build the review screen. 5. Audit every restatement and resolution.
**Tests:** `tests/integration/test_restatement.py` — `test_restatement_creates_version_keeps_old`, `test_dependent_tests_flagged_for_recomputation`, `test_corrected_quarantine_row_carries_both_provenances`, `test_rejected_row_retained_with_reason`, `test_any_value_resolves_to_source_row_and_mapping`, `test_restatement_audited`.
**Run:** `pytest -q tests/integration/test_restatement.py` 6 passed.
**Done when:** the six tests pass and every stored statement value resolves to its source.
**Evidence:** a provenance trace for one value, a restatement audit event.

---

### [x] `T-027` · Ratio library part 1 — leverage, coverage and liquidity
`Milestone: M-1` · `Builds: R-07` · `Days: 1.5` · `Depends on: T-024` · `Snapshot: var/snapshots/T-027/` · `Build: #2 · Phase 1 · cum 0.7h`

**Goal:** twelve of the twenty-four definitions, computed exactly, in pure code, with the plausible bands as data.
**Context:** `spec §R-07` names twenty-four. This task covers leverage, DSCR, interest coverage, fixed-charge coverage, current ratio, quick ratio, TOL/TNW, debt/EBITDA, net debt/EBITDA, EBITDA margin, TNW floor and minimum net worth. **No model is involved in this file or any file it imports.** Plausible bands live in the `ratio_definition` table so `spec §17.5`'s claim that they are configuration is true.
**Read first:** `plan.md §6` (`C-30`), `src/covenant_radar/domain/statements/chart.py`, `db/seed/data/`.
**Contracts:** `C-30` `compute_ratio`.
**Files owned:** `src/covenant_radar/domain/ratios/__init__.py`, `definitions.py`, `library.py`, `compute.py`, `src/covenant_radar/db/seed/data/ratio_definitions.json` (the twelve), `tests/unit/test_ratios_1.py`
**Behaviour:** `compute_ratio` dispatches on the definition, returns a `RatioResult` carrying the value, whether it is computable, the reason when it is not, and every input line it read with its value — because the trace and the why-panel read `inputs_used` and an explanation that cannot name its inputs is not one.
**Every case:** a missing line → not computable naming it; a zero or sign-meaningless denominator → not computable with the documented reason, never an arithmetic exception; a value outside the definition's plausible band → still computed and returned, with the band breach flagged, because a real borrower may genuinely be there and it is the *threshold* that is implausible, not the observation; an unknown definition → `UnknownDefinition`; a `Decimal` throughout with no float anywhere in the path.
**Steps:** 1. Define the twelve with formula, required lines, unit, plausible band and direction hint. 2. Implement one pure function per ratio, none raising. 3. Implement `compute_ratio` with dispatch, the not-computable paths and `inputs_used`. 4. Seed the definitions. 5. Hand-work two cases per ratio from the fixture statements and encode them as exact-equality tests.
**Tests:** `tests/unit/test_ratios_1.py` — `test_twelve_definitions_present`, `test_each_ratio_hand_worked_exact` (parameterised, 24 cases), `test_missing_line_named`, `test_zero_denominator_not_computable`, `test_unknown_definition_raises`, `test_inputs_used_lists_every_line_read`, `test_no_float_in_computation_path`.
**Run:** `pytest -q tests/unit/test_ratios_1.py` all passed with 24 hand-worked cases exact.
**Done when:** every hand-worked case matches exactly and no ratio path can raise an arithmetic error.
**Evidence:** the hand-worked case table with expected and actual values.

---

### [x] `T-028` · Ratio library part 2 — conduct, working capital and covenant conditions
`Milestone: M-1` · `Builds: R-07` · `Days: 1.5` · `Depends on: T-027` · `Snapshot: var/snapshots/T-028/` · `Build: #3 · Phase 1 · cum 1.1h`

**Goal:** the remaining twelve definitions, including the ones that read facility and conduct data rather than statements.
**Context:** utilisation, drawing-power headroom, receivable days, inventory days, payable days, cash conversion cycle, working-capital gap, promoter shareholding floor, dividend restriction, asset-cover ratio, minimum liquidity and maximum capex. Several are conditions rather than ratios — a promoter-shareholding floor is a covenant condition with a boolean outcome — and the result type carries both shapes so the engine treats them uniformly.
**Read first:** `src/covenant_radar/domain/ratios/compute.py`, `plan.md §5.2` (`facility`, `facility_conduct`).
**Contracts:** `C-30`, including its `FacilityFacts` argument.
**Files owned:** `src/covenant_radar/domain/ratios/definitions.py` (extended), `conditions.py`, `src/covenant_radar/db/seed/data/ratio_definitions.json` (the remaining twelve), `tests/unit/test_ratios_2.py`
**Behaviour:** the twelve computed from statement lines, facility facts or both; condition-type definitions returning a comparable value plus a boolean outcome so `C-32` can evaluate them without a special case.
**Every case:** a ratio needing facility facts called without them → not computable naming what was missing; a period with zero revenue for a days-based ratio → not computable rather than infinite; a condition definition evaluated → returns both the value and the outcome; utilisation above 100 → returned as measured and flagged, because an excess is a real and important state, not a data error to be clamped away.
**Steps:** 1. Define the twelve with their inputs. 2. Implement them, including the condition shape. 3. Extend `compute_ratio` for the facility-facts argument. 4. Seed the definitions. 5. Hand-work two cases per definition.
**Tests:** `tests/unit/test_ratios_2.py` — `test_twenty_four_definitions_total`, `test_each_hand_worked_exact` (parameterised, 24 cases), `test_facility_facts_required_named_when_absent`, `test_zero_revenue_days_ratio_not_computable`, `test_condition_returns_value_and_outcome`, `test_utilisation_above_100_returned_and_flagged`.
**Run:** `pytest -q tests/unit/test_ratios_2.py` all passed · `python -c "from covenant_radar.domain.ratios.library import LIBRARY; assert len(LIBRARY)==24"` exit 0.
**Done when:** all twenty-four definitions exist and every hand-worked case is exact.
**Evidence:** the full hand-worked case table.

---

### [x] `T-029` · Custom formula parser, validator and restricted evaluator
`Milestone: M-1` · `Builds: R-07` · `Days: 1.5` · `Depends on: T-027` · `Snapshot: var/snapshots/T-029/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** a bank can express a covenant the library does not name, without the product gaining an arbitrary code-execution surface.
**Context:** `spec §R-07.d`. The formula is parsed into a restricted syntax tree — literals, known statement line names, the four arithmetic operators and parentheses — and anything else is refused **before evaluation**, naming the disallowed construct. This is a security boundary, not a convenience feature.
**Read first:** `plan.md §6` (`C-31`), `src/covenant_radar/domain/ratios/compute.py`, `domain/statements/chart.py`.
**Contracts:** `C-31` `parse_custom_formula`.
**Files owned:** `src/covenant_radar/domain/ratios/custom.py`, `tests/unit/test_custom_formula.py`, `tests/property/test_custom_formula_safety.py`
**Behaviour:** `parse_custom_formula(text, allowed_lines)` returns a `Formula` that evaluates against a line mapping and reports its required lines; the parse refuses any call, attribute access, subscript, comprehension, lambda, import, walrus, or name outside the allowed set.
**Every case:** any disallowed construct → `FormulaRefused` naming the construct and its position, before evaluation; an unknown line name → refused naming it and listing near matches; a division by zero at evaluation → not computable with the documented reason, never an exception; a formula longer than the configured limit or deeper than the configured nesting → refused; a formula that parses but references no line → refused, because a constant is not a covenant.
**Steps:** 1. Parse with the standard library's own parser and walk the tree with an allow-list of node types. 2. Resolve and validate names against `allowed_lines`. 3. Compile to a closure over `Decimal` arithmetic with no builtins in scope. 4. Enforce length and depth limits. 5. Write a property test that generates hostile inputs and asserts refusal.
**Tests:** `tests/unit/test_custom_formula.py` — `test_valid_formula_evaluates`, `test_call_refused_naming_construct`, `test_attribute_refused`, `test_subscript_refused`, `test_import_refused`, `test_unknown_line_refused_with_suggestions`, `test_division_by_zero_not_computable`, `test_depth_and_length_limits`, `test_constant_only_formula_refused`; `tests/property/test_custom_formula_safety.py` — `test_no_generated_input_ever_executes_code`.
**Run:** `pytest -q tests/unit/test_custom_formula.py tests/property/test_custom_formula_safety.py` all passed.
**Done when:** every hostile construct is refused before evaluation and the property test finds no escape.
**Evidence:** the refusal test output listing every construct covered.

---

### [x] `T-030` · Not-computable and missing-line behaviour across the library
`Milestone: M-1` · `Builds: R-07` · `Days: 0.5` · `Depends on: T-028` · `Snapshot: var/snapshots/T-030/` · `Build: #4 · Phase 1 · cum 1.2h`

**Goal:** every one of the twenty-four definitions behaves identically when it cannot compute, with one message vocabulary the engine, the screens and the memo all reuse.
**Context:** `spec §R-07.b`, `R-07.c` and `R-08.d`. Inconsistent failure messages become inconsistent screens, and an auditor reading two different phrasings for the same condition reasonably asks which is right.
**Read first:** `src/covenant_radar/domain/ratios/compute.py`, `conditions.py`.
**Contracts:** `C-30`'s `RatioResult`.
**Files owned:** `src/covenant_radar/domain/ratios/reasons.py`, `compute.py` (the reason paths), `tests/unit/test_not_computable.py`
**Behaviour:** an enumerated reason set with one stable code and one translatable message each; every definition returning a reason from that set and never a free-text string.
**Every case:** each of the twenty-four definitions, driven into each applicable failure mode, returns the enumerated reason; a free-text reason anywhere → the test fails; a reason with no translation entry → the build check fails.
**Steps:** 1. Enumerate the reasons: missing line, zero denominator, sign-meaningless denominator, period incomplete, facility facts absent, formula not computable. 2. Replace every reason string with the enumeration. 3. Add the translation entries. 4. Parameterise a test across all twenty-four definitions and every applicable mode.
**Tests:** `tests/unit/test_not_computable.py` — `test_every_definition_uses_enumerated_reason` (parameterised across 24), `test_no_free_text_reason_in_source`, `test_every_reason_has_a_translation`.
**Run:** `pytest -q tests/unit/test_not_computable.py` all passed.
**Done when:** no free-text not-computable reason exists in the source and every enumerated reason is translated.
**Evidence:** the parameterised test output.

---

### [x] `T-031` · Covenant registry: model, versioning, immutability enforcement
`Milestone: M-1` · `Builds: R-05` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-031/` · `Build: #5 · Phase 1 · cum 1.7h`

**Goal:** the contract's terms as the product's ground truth — versioned, effective-dated, and unalterable once anything has been tested against them.
**Context:** `spec §R-05.a`. The trigger from `T-009` is one of three enforcement points; this task supplies the second (no repository method exists to update a frozen column) and the third (a test that proves both).
**Read first:** `plan.md §5.5`, `src/covenant_radar/db/models/covenant.py`, `domain/ratios/library.py`.
**Contracts:** the registry service `C-06` and `C-07` call.
**Files owned:** `src/covenant_radar/domain/covenants/model.py`, `src/covenant_radar/db/repositories/covenant.py`, `src/covenant_radar/services/registry.py` (register and amend), `tests/unit/test_registry_model.py`, `tests/integration/test_registry_versioning.py`
**Behaviour:** `register` creates version 1 in draft or live per the maker-checker setting; `amend` creates the next version, closes the prior with `effective_to` and marks it superseded, in one transaction; `live_at(facility, date)` returns the versions in force on that date; historical tests continue to reference the version they used.
**Every case:** an amendment to a tested version → a new version, the old intact, and any attempt to alter the old's terms refused by both the repository and the trigger; overlapping effective ranges for the same covenant → refused naming both versions; a definition outside the library that is not a valid custom formula → refused; a frequency, direction or unit outside its allowed set → refused naming the set; `live_at` for a date before any version → empty, not the earliest version.
**Steps:** 1. Write the domain model for a covenant version as a frozen value object. 2. Write the repository with no update method for frozen columns. 3. Implement register and amend transactionally. 4. Implement `live_at` and effective-range validation. 5. Prove all three enforcement points in tests.
**Tests:** `tests/unit/test_registry_model.py` — `test_version_is_frozen_value_object`, `test_allowed_sets_enforced`; `tests/integration/test_registry_versioning.py` — `test_amend_creates_version_old_intact`, `test_historical_test_references_old_version`, `test_repository_has_no_update_for_frozen_columns`, `test_trigger_refuses_direct_update`, `test_overlapping_ranges_refused`, `test_live_at_before_first_version_is_empty`.
**Run:** `pytest -q tests/unit/test_registry_model.py tests/integration/test_registry_versioning.py` 8 passed.
**Done when:** the eight tests pass and all three immutability enforcement points are demonstrated.
**Evidence:** the test output showing the trigger refusal.

---

### [x] `T-032` · Exceptions, waivers, cure and grace periods
`Milestone: M-1` · `Builds: R-05` · `Days: 1.5` · `Depends on: T-031` · `Snapshot: var/snapshots/T-032/` · `Build: #6 · Phase 1 · cum 2.1h`

**Goal:** the dated, reasoned, approved objects that make a covenant test right in the real world, where thresholds are relaxed for two quarters and breaches have thirty days to cure.
**Context:** `spec §R-05.c` and `R-05.e`. An exception belongs to a version and relaxes its threshold inside a window. A waiver attaches to the covenant without altering any version, because a waiver is a decision about a breach and not a change to the contract. A cure period turns a failing test into `breach_cure_open` until the window closes without a passing retest.
**Read first:** `plan.md §5.5`, `src/covenant_radar/services/registry.py`.
**Contracts:** `C-32`'s exception and waiver arguments.
**Files owned:** `src/covenant_radar/domain/covenants/exceptions.py`, `cure.py`, `src/covenant_radar/services/registry.py` (exception and waiver paths), `tests/unit/test_exceptions_cure.py`, `tests/integration/test_waivers.py`
**Behaviour:** `resolve_exception(version, period)` returns the exception in force or none; `resolve_waiver(covenant, date)` returns the approved waiver in force or none; `cure_state(test, retests, thresholds)` returns open, cured or confirmed with the window end date.
**Every case:** a period exactly at an exception window's boundary → inside, and the convention is documented and tested at both ends; two overlapping exceptions on one version → refused at registration; a waiver requested but not approved → not in force, and a test during that period uses the base threshold; a cure window closing with no retest → `breach_confirmed`, because absence of a retest is not a cure; a passing retest inside the window → `cured`, with both states retained; a cure period on a covenant whose frequency is longer than the window → refused at registration naming the inconsistency.
**Steps:** 1. Implement exception resolution with boundary inclusivity. 2. Implement waiver resolution with the approval requirement. 3. Implement cure-state computation over a test and its retests. 4. Add registration-time validation for overlaps and inconsistent cure windows. 5. Audit waiver request, approval and rejection.
**Tests:** `tests/unit/test_exceptions_cure.py` — `test_boundary_periods_inside_window`, `test_overlapping_exceptions_refused`, `test_cure_open_then_confirmed_without_retest`, `test_passing_retest_cures_and_keeps_both_states`, `test_cure_window_shorter_than_frequency_refused`; `tests/integration/test_waivers.py` — `test_unapproved_waiver_not_in_force`, `test_approved_waiver_named_in_test_record`, `test_waiver_lifecycle_audited`.
**Run:** `pytest -q tests/unit/test_exceptions_cure.py tests/integration/test_waivers.py` 8 passed.
**Done when:** the eight tests pass and every boundary is tested at its exact value.
**Evidence:** the boundary test output.

---

### [x] `T-033` · Registry service, maker-checker path and API
`Milestone: M-1` · `Builds: R-05` · `Days: 1.5` · `Depends on: T-032, T-018` · `Snapshot: var/snapshots/T-033/` · `Build: #7 · Phase 1 · cum 2.4h`

**Goal:** covenants are registered, amended, waived and retired through one service with the approval path wired, and the same operations are available on the API.
**Context:** `spec §16.1` gives registering and approving to different roles. `spec §R-05.d`: the same actor may not do both.
**Read first:** `src/covenant_radar/security/maker_checker.py`, `services/registry.py`, `plan.md §6` (`C-06`, `C-07`, `C-21`).
**Contracts:** `C-06`, `C-07`, `C-21`'s covenant resources, `C-60`.
**Files owned:** `src/covenant_radar/services/registry.py` (completion), `src/covenant_radar/api/v1/routers/covenants.py`, `api/v1/schemas/covenants.py`, `src/covenant_radar/web/routes/covenants.py`, `web/templates/screens/covenants/*`, `tests/integration/test_registry_service.py`
**Behaviour:** register, amend, request and approve a waiver, and retire, each declaring its permission, each routed through maker-checker where configured, each audited, each exposed on the web and the API with identical rules.
**Every case:** the maker approving their own request → `409` naming the rule; a registration approved → the version becomes live and is immediately eligible for testing; a registration rejected → the draft is retained with the rejection reason, not deleted, because the rejection is evidence; retiring a covenant with open cure state → refused naming the state; an API caller without the permission → `403` naming it; a covenant listed by a scoped user → only within their scope.
**Steps:** 1. Complete the service with the four operations and their permissions. 2. Register the maker-checker application callbacks. 3. Write the API router and schemas. 4. Write the web routes and screens. 5. Audit every operation and decision.
**Tests:** `tests/integration/test_registry_service.py` — `test_maker_cannot_approve`, `test_approved_version_immediately_testable`, `test_rejected_draft_retained_with_reason`, `test_retire_with_open_cure_refused`, `test_permission_enforced_on_api`, `test_scope_enforced_on_list`, `test_every_operation_audited`.
**Run:** `pytest -q tests/integration/test_registry_service.py` 7 passed.
**Done when:** the seven tests pass and the web and API surfaces enforce identical rules.
**Evidence:** the test output, an approval audit trail sample.

---

### [x] `T-034` · Covenant engine: evaluation, headroom, verdicts, boundaries
`Milestone: M-1` · `Builds: R-08` · `Days: 2.5` · `Depends on: T-031, T-030` · `Snapshot: var/snapshots/T-034/` · `Build: #8 · Phase 1 · cum 3.0h`

**Goal:** the contractual condition computed exactly, on the right threshold, with signed headroom and a verdict from a closed set — the record every later number is defended against.
**Context:** `spec §R-08` and `spec §17.5`'s boundary conventions. Headroom is signed distance to the threshold as a percentage of the threshold, computed by direction. This is the single most important computation in the product, and it is pure code with no model anywhere in its import graph.
**Read first:** `plan.md §6` (`C-32`), `spec §17.5`, `src/covenant_radar/domain/ratios/compute.py`, `domain/covenants/exceptions.py`.
**Contracts:** `C-32` `evaluate_covenant`.
**Files owned:** `src/covenant_radar/domain/covenants/evaluate.py`, `headroom.py`, `src/covenant_radar/services/engine.py`, `tests/unit/test_engine.py`, `tests/property/test_headroom_invariants.py`
**Behaviour:** `evaluate_covenant` returns the value, the threshold actually used, signed headroom, the verdict, the exception and waiver applied, the cure end date, and the thresholds compared with the side each value fell — the last of which is what `T-037`'s trace and the why-panel render.
**Every case:** value exactly at threshold → headroom zero and the documented boundary verdict, identical on every path; a not-computable ratio → verdict `not_computable` with the enumerated reason and no invented value; an incomplete period → verdict `stale` naming the last complete period; an exception in force → the relaxed threshold used and named; a waiver in force → recorded and the verdict reflecting it; direction `min` versus `max` → headroom signs correct in both, proven by property test; a warning headroom configured → verdict `warning` between it and the threshold.
**Steps:** 1. Implement headroom by direction as a pure function. 2. Implement the verdict decision table with every case enumerated and none defaulting. 3. Implement threshold resolution through exception and waiver. 4. Build the thresholds-compared record with sides. 5. Write the service that loads, evaluates and persists a test row. 6. Hand-work twelve engine cases across directions, exceptions, staleness and boundaries.
**Tests:** `tests/unit/test_engine.py` — `test_twelve_hand_worked_cases_exact`, `test_value_at_threshold_boundary`, `test_not_computable_reason_carried`, `test_incomplete_period_marks_stale_naming_last`, `test_exception_threshold_used_and_named`, `test_waiver_recorded`, `test_warning_band_between_warning_and_threshold`, `test_thresholds_compared_carries_side`; `tests/property/test_headroom_invariants.py` — `test_headroom_sign_correct_for_both_directions`, `test_headroom_zero_iff_value_equals_threshold`, `test_headroom_monotonic_in_value`.
**Run:** `pytest -q tests/unit/test_engine.py tests/property/test_headroom_invariants.py` all passed with 12 hand-worked cases exact.
**Done when:** every hand-worked case is exact, every property holds, and the verdict table has no default branch.
**Evidence:** the hand-worked case table with expected and actual values.

---

### [x] `T-035` · Testing calendar, scheduling and on-arrival retest
`Milestone: M-1` · `Builds: R-08` · `Days: 1.5` · `Depends on: T-034` · `Snapshot: var/snapshots/T-035/` · `Build: implemented outside the original build window — pytest -q tests/unit/test_calendar.py tests/integration/test_retest_on_arrival.py green`

**Goal:** covenants are tested when the contract says, and again whenever the data they depend on changes, without either being forgotten.
**Context:** `spec §R-08` requires both. The calendar is also what `R-09`'s certificate requests derive from and what `R-28`'s nightly pipeline walks.
**Read first:** `plan.md §5.5` (`covenant_schedule`), `src/covenant_radar/services/engine.py`, `db/seed/data/calendar.json`.
**Contracts:** the schedule rows `C-14` and `T-121` read.
**Files owned:** `src/covenant_radar/domain/covenants/calendar.py`, `src/covenant_radar/services/engine.py` (scheduling and retest), `tests/unit/test_calendar.py`, `tests/integration/test_retest_on_arrival.py`
**Behaviour:** due dates generated from frequency, effective range and the FY calendar, with holiday and weekend adjustment by the configured convention; a retest triggered when a statement, restatement, waiver, exception or conduct change touches a covenant's inputs; schedule states of due, tested, missed and not-applicable.
**Every case:** a covenant effective mid-quarter → its first due date computed by the documented rule, not assumed; a due date falling on a holiday → adjusted by the configured convention and the adjustment recorded; a restatement of a period already tested → a retest queued, the prior test retained, and both visible; a covenant retired mid-window → remaining due dates marked not-applicable rather than deleted; an on-event frequency → no calendar entries, tested only on arrival.
**Steps:** 1. Implement due-date generation with the FY calendar and holiday adjustment. 2. Implement schedule state transitions. 3. Implement dependency detection so a data change resolves to the covenants it affects. 4. Queue retests idempotently. 5. Retain prior tests and mark supersession.
**Tests:** `tests/unit/test_calendar.py` — `test_first_due_date_for_mid_period_start`, `test_holiday_adjustment_recorded`, `test_retired_covenant_marks_remaining_not_applicable`, `test_on_event_frequency_has_no_calendar`; `tests/integration/test_retest_on_arrival.py` — `test_restatement_queues_retest_and_keeps_prior`, `test_conduct_change_triggers_affected_covenants_only`, `test_retest_queueing_is_idempotent`.
**Run:** `pytest -q tests/unit/test_calendar.py tests/integration/test_retest_on_arrival.py` 7 passed.
**Done when:** the seven tests pass and no data change that affects a covenant leaves it untested.
**Evidence:** a generated calendar for one covenant, a retest queue trace.

---

### [x] `T-036` · SMA banding from account conduct
`Milestone: M-1` · `Builds: R-08` · `Days: 1.0` · `Depends on: T-034` · `Snapshot: var/snapshots/T-036/` · `Build: DONE — pulled forward ahead of schedule`

**Goal:** the product speaks the supervisory vocabulary natively, because SMA-0/1/2 is how the desk and the regulator both describe this borrower.
**Context:** `spec §2.1`'s Prudential Framework: 1–30, 31–60 and 61–90 days overdue. `spec §R-08.f`. The band is derived from conduct, recorded with its derivation, and feeds both the queue and the CRILC export.
**Read first:** `plan.md §6` (`C-33`), `plan.md §5.2` (`facility_conduct`), `spec §2.1`.
**Contracts:** `C-33` `sma_band`.
**Files owned:** `src/covenant_radar/domain/covenants/sma.py`, `src/covenant_radar/services/engine.py` (the banding path), `tests/unit/test_sma.py`
**Behaviour:** `sma_band(days_past_due)` returns the band; the service derives it per facility per day from conduct, records the derivation in the trace, and computes the borrower band as the worst across facilities.
**Every case:** exactly 1, 30, 31, 60, 61 and 90 days → the documented band at each boundary, tested at every one; zero days → no band; more than 90 → beyond the SMA range, reported as such rather than clamped to SMA-2; negative days → `ValueError`, because that is a data defect and must be visible; a facility with no conduct for the day → no band with the reason recorded, never assumed current.
**Steps:** 1. Implement the pure banding function with every boundary. 2. Implement per-facility derivation from conduct. 3. Implement the borrower roll-up as the worst band. 4. Record the derivation for the trace. 5. Test every boundary value explicitly.
**Tests:** `tests/unit/test_sma.py` — `test_every_boundary_value` (parameterised over 0, 1, 30, 31, 60, 61, 90, 91), `test_negative_days_raises`, `test_borrower_band_is_worst_across_facilities`, `test_missing_conduct_records_reason_not_assumption`.
**Run:** `pytest -q tests/unit/test_sma.py` all passed.
**Done when:** every boundary is tested at its exact value and no missing conduct is treated as current.
**Evidence:** the boundary test output.

---

### [x] `T-037` · Stage-2 trace rows and engine explainability data
`Milestone: M-1` · `Builds: R-08` · `Days: 1.5` · `Depends on: T-034` · `Snapshot: var/snapshots/T-037/` · `Build: #9 · Phase 1 · cum 3.4h`

**Goal:** every covenant test writes the record the why-panel will read, in the one shape every other stage will also use — written now, because retrofitting it after the screens exist is a rewrite of both.
**Context:** `plan.md §8.6`. One row per stage per subject carrying inputs, outputs, decider, the rule version, the thresholds compared **with which side each value fell**, the confidence and the sources. Stage 2 is the first user of the shape, so this task also fixes it.
**Read first:** `plan.md §6` (`C-41`), `plan.md §5.9` (`trace_row`), `src/covenant_radar/domain/covenants/evaluate.py`.
**Contracts:** `C-41` `stage_record`.
**Files owned:** `src/covenant_radar/domain/trace.py`, `src/covenant_radar/db/repositories/trace.py`, `src/covenant_radar/services/engine.py` (the trace write), `tests/unit/test_trace_shape.py`, `tests/integration/test_stage2_trace.py`
**Behaviour:** `stage_record(...)` builds a validated record; the repository persists it; `read(subject)` returns every stage in order with missing stages present and marked not-run.
**Every case:** a `thresholds_compared` entry without a side → `ValueError` naming the entry, because a threshold with no side is a coin toss written as a number; a stage number outside the defined range → `ValueError`; a non-serialisable input → coerced to its string form rather than losing the row, with the coercion noted; two writes for the same subject and stage → both retained, the later one shown, because nothing in the audit path is overwritten; `read` for a subject with no rows → every stage returned as not-run rather than an empty list.
**Steps:** 1. Define the record with its eleven fields and the side enumeration. 2. Implement validation. 3. Implement the repository write and the ordered read with not-run padding. 4. Emit a stage-2 row from every covenant test with the rule version and the thresholds compared. 5. Test the shape independently of any stage so later stages inherit it.
**Tests:** `tests/unit/test_trace_shape.py` — `test_entry_without_side_raises`, `test_stage_out_of_range_raises`, `test_non_serialisable_coerced_not_lost`, `test_read_pads_missing_stages_as_not_run`, `test_code_and_model_stages_share_one_field_set`; `tests/integration/test_stage2_trace.py` — `test_every_test_writes_one_stage2_row`, `test_row_names_threshold_value_observed_and_side`.
**Run:** `pytest -q tests/unit/test_trace_shape.py tests/integration/test_stage2_trace.py` 7 passed.
**Done when:** the seven tests pass and every covenant test has a trace row naming its thresholds and sides.
**Evidence:** a sample stage-2 trace row.

---

### [x] `T-038` · Certificate model and request generation from the calendar
`Milestone: M-1` · `Builds: R-09` · `Days: 1.5` · `Depends on: T-035` · `Snapshot: var/snapshots/T-038/` · `Build: implemented outside the original build window — pytest -q tests/unit/test_certificate_requirements.py tests/integration/test_certificate_generation.py green`

**Goal:** the compliance certificates the covenant register implies are requested ahead of time rather than chased afterwards.
**Context:** `spec §R-09`. RBI inspections found monitoring reliant on certificates that arrive late; the product's contribution is to know which are due, when, and from whom.
**Read first:** `plan.md §5.6` (`certificate_request`), `src/covenant_radar/domain/covenants/calendar.py`.
**Contracts:** the request rows `T-117`'s digest reads.
**Files owned:** `src/covenant_radar/domain/certificates/__init__.py`, `requirements.py`, `src/covenant_radar/services/certificates.py` (generation), `src/covenant_radar/db/repositories/certificate.py`, `tests/unit/test_certificate_requirements.py`, `tests/integration/test_certificate_generation.py`
**Behaviour:** requirements derived from covenants whose testing basis is a borrower certificate; requests raised at the configured lead time with the due date, the covenant and the borrower contact; generation idempotent so a repeated run creates nothing new.
**Every case:** several covenants sharing one certificate → one request covering all of them, not one each; a covenant retired before its request's due date → the request cancelled with the reason; a borrower with no contact → the request raised anyway and assigned to the relationship manager, because the absence of a contact is not a reason to skip the control; generation run twice → no duplicates; a lead time longer than the frequency → refused at configuration.
**Steps:** 1. Derive requirements from the register. 2. Implement request generation with grouping and idempotence. 3. Implement cancellation on covenant retirement. 4. Handle the missing-contact case explicitly. 5. Validate the lead-time configuration.
**Tests:** `tests/unit/test_certificate_requirements.py` — `test_covenants_grouped_into_one_request`, `test_lead_time_longer_than_frequency_refused`; `tests/integration/test_certificate_generation.py` — `test_generation_idempotent`, `test_retired_covenant_cancels_request`, `test_missing_contact_assigns_to_rm`, `test_due_dates_match_calendar`.
**Run:** `pytest -q tests/unit/test_certificate_requirements.py tests/integration/test_certificate_generation.py` 17 passed (the six named tests plus additional unit and integration cases covering grouping determinism, lead-time boundaries and non-certificate covenants).
**Done when:** the six tests pass and repeated generation creates no duplicate.
**Evidence:** a generated request set for the fixture portfolio.

---

### [x] `T-039` · Certificate receipt, linkage, rejection and overdue evidence
`Milestone: M-1` · `Builds: R-09` · `Days: 1.0` · `Depends on: T-038` · `Snapshot: var/snapshots/T-039/` · `Build: implemented outside the original build window — pytest -q tests/integration/test_certificate_lifecycle.py green`

**Goal:** a received certificate is linked to the tests that consume it, a rejected one un-links cleanly, and an overdue one becomes a monitoring signal rather than an administrative footnote.
**Context:** `spec §R-09.c` and `R-09.d`. The overdue evidence item is created here in shape, and `T-046` gives it its scoring behaviour.
**Read first:** `src/covenant_radar/services/certificates.py`, `plan.md §5.6`.
**Contracts:** `C-60`; the evidence rows `T-046` consumes.
**Files owned:** `src/covenant_radar/services/certificates.py` (lifecycle), `src/covenant_radar/web/routes/certificates.py`, `web/templates/screens/certificates/*`, `tests/integration/test_certificate_lifecycle.py`
**Behaviour:** receipt links an uploaded document to the request; review accepts or rejects with a reason; acceptance links the certificate to the covenant tests that consume it; rejection removes the link, flags affected tests for recomputation and retains both states; a request past its due date plus grace becomes an overdue evidence item.
**Every case:** an accepted certificate later found to be for the wrong period → rejected with a reason, links removed, tests flagged, both states in the trail; a certificate received after the covenant was tested without it → the test flagged for recomputation, not silently left; an overdue request later satisfied → the evidence item superseded rather than deleted; a document linked to two requests → permitted, because one certificate genuinely covers several covenants, and both links recorded.
**Steps:** 1. Implement receipt with document linkage. 2. Implement review, acceptance and rejection with the recomputation flag. 3. Implement overdue detection and evidence creation. 4. Build the certificate screen. 5. Audit every transition.
**Tests:** `tests/integration/test_certificate_lifecycle.py` — `test_acceptance_links_to_consuming_tests`, `test_rejection_unlinks_and_flags_recomputation`, `test_late_receipt_flags_prior_test`, `test_overdue_creates_evidence_item`, `test_satisfied_overdue_supersedes_not_deletes`, `test_one_document_covers_several_requests`.
**Run:** `pytest -q tests/integration/test_certificate_lifecycle.py` 8 passed (the six named tests plus two additional refusal cases).
**Done when:** the six tests pass and no state transition deletes a prior state.
**Evidence:** a certificate lifecycle audit trail. The screen (`web/routes/certificates.py`, `web/templates/screens/certificates/index.html`) is built, lint/type-clean and renders correctly in isolation, but is not wired into `web/application.py`'s live router list: `services/ledger.py` (owned by the already-implemented `T-047`, out of this task's `Files owned`) requires a real `sqlalchemy.orm.Session` in its constructor, while every other web-facing service in `application.py` is composed over a `scoped_session` proxy — wiring this router in as-is would crash the first time an overdue sweep ran through the request-scoped session. Fixing that needs a change to `services/ledger.py` itself, which is out of this task's scope.

---

### [x] `T-040` · Reference portfolio: borrowers, facilities, financials
`Milestone: M-1` · `Supports: spec §22` · `Days: 2.5` · `Depends on: T-011` · `Snapshot: var/snapshots/T-040/` · `Build: #10 · Phase 1 · cum 4.0h`

**Goal:** a deterministically generated synthetic Indian commercial-lending book that every later gate, the evaluation harness, the performance suite and the customer evaluation build all read from.
**Context:** `spec §22.1`. Synthetic and clearly labelled as such, but its *shapes* are real: Indian entity names at realistic lengths, CIN-format identifiers, ₹ crore facilities at the CRILC band, ratio ranges calibrated to published aggregates, Indian FY quarters. A book generated from the same seed twice must be identical, because otherwise no cohort gate is reproducible.
**Read first:** `spec §22.1`, `spec §2` row 11, `src/covenant_radar/db/seed/loader.py`, `domain/statements/chart.py`.
**Contracts:** `C-72` `radarctl seed --reference-portfolio`.
**Files owned:** `evaluation/reference_portfolio/__init__.py`, `generator.py`, `names.py`, `financials.py`, `src/covenant_radar/cli.py` (the reference-portfolio flag), `tests/integration/test_reference_portfolio.py`
**Behaviour:** generates 5,000 borrowers, 12,000 facilities and eight quarters of financials from a fixed seed, with ratio ranges inside published aggregate bands and every facility at or above the CRILC threshold; regenerating from the same seed produces content-identical data.
**Every case:** generated twice from the same seed → identical content hashes for every table; a generated ratio outside its plausible band → raised at generation time rather than written, because an implausible book invalidates every later gate; a generated entity name shorter than a realistic Indian legal name → refused, because layouts must be exercised at real lengths; generation into a non-empty database → refused without an explicit flag; a smaller size requested for development → supported and still deterministic.
**Steps:** 1. Implement the seeded generator with an explicit random source and no clock or environment input. 2. Generate borrowers, groups, related parties and contacts with realistic Indian names and CIN-format identifiers. 3. Generate facilities at the CRILC band with types and limits. 4. Generate eight quarters of statements with ratio ranges inside the aggregate bands. 5. Add determinism verification by content hash. 6. Wire the CLI flag with the non-empty guard.
**Tests:** `tests/integration/test_reference_portfolio.py` — `test_two_runs_content_identical`, `test_sizes_match_specification`, `test_every_facility_at_or_above_crilc_band`, `test_every_ratio_inside_plausible_band`, `test_names_at_realistic_lengths`, `test_generation_into_non_empty_refused`.
**Run:** `radarctl seed --reference-portfolio` exit 0 · `radarctl seed --check-deterministic` prints identical hashes · `pytest -q tests/integration/test_reference_portfolio.py` 6 passed.
**Done when:** two generations are content-identical and every generated figure is inside its plausible band.
**Evidence:** the two content-hash sets, a sample borrower record.

---

### [x] `T-041` · Reference portfolio: cohorts, signals and labelled outcomes
`Milestone: M-1` · `Supports: spec §22` · `Days: 2.0` · `Depends on: T-040` · `Snapshot: var/snapshots/T-041/` · `Build: #11 · Phase 1 · cum 4.5h`

**Goal:** the labelled cohorts every intelligence gate is judged against — deteriorating borrowers with known breach dates, noisy-transient borrowers that must never escalate, and stable borrowers that must stay quiet.
**Context:** `spec §6` G1 and G3 are a deliberate pair, and this is the data that proves both. Without labels there is no way to say the forecast is early *and* quiet, which is the entire claim.
**Read first:** `evaluation/reference_portfolio/generator.py`, `spec §6` (G1, G3), `spec §17.7`.
**Contracts:** the labels `T-104` scores against.
**Files owned:** `evaluation/reference_portfolio/cohorts.py`, `signals.py`, `labels.py`, `tests/integration/test_cohorts.py`
**Behaviour:** three authored cohorts plus a templated remainder; 365 days of daily signal events across all six families per borrower; a labels file recording, per deteriorating borrower, the covenant that breaches and the date it does, derived from the generated trajectory rather than asserted independently.
**Every case:** a deteriorating borrower's generated trajectory not actually reaching its labelled breach date → generation fails, because a label that disagrees with the data would silently pass every later gate; a noisy-transient borrower whose blips would cross the persistence rule → refused at generation, since the cohort's whole purpose is to stay below it; a stable borrower drifting outside its band → refused; every cohort assignment recorded on the borrower so a test can filter by it; generation deterministic with `T-040`'s seed.
**Steps:** 1. Implement the deteriorating profile: lengthening payment delays, climbing utilisation, a covenant trajectory that crosses on a computed date. 2. Implement the noisy-transient profile with blips that decay. 3. Implement the stable profile. 4. Generate the templated remainder from the same distributions. 5. Generate 365 days of events across the six families. 6. Derive and write the labels from the generated data, and verify each by recomputation.
**Tests:** `tests/integration/test_cohorts.py` — `test_cohort_counts_and_assignment`, `test_deteriorating_trajectory_reaches_labelled_date`, `test_noisy_cohort_never_meets_persistence_rule`, `test_stable_cohort_stays_in_band`, `test_all_six_signal_families_present`, `test_labels_derived_not_asserted`, `test_deterministic_with_generator_seed`.
**Run:** `pytest -q tests/integration/test_cohorts.py` 7 passed.
**Done when:** the seven tests pass and every label is verified by recomputation against the generated data. **M-1's gate can now be demonstrated.**
**Evidence:** the cohort summary, the labels file, the M-1 gate record.

---

### M-2 · Intelligence — 35.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-042` · Signal event model and the ingestion framework
`Milestone: M-2` · `Builds: R-10` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-042/` · `Build: #25 · Phase 3 · cum 12.0h`

**Goal:** typed behavioural events land reliably from any source, keyed so redelivery is free of duplicates, and the six families are a closed set the rest of the product can rely on.
**Context:** `spec §R-10`. The families are payment, utilisation, treasury, concentration, industry and news. `signal_event.content_hash` is unique, which is what makes idempotence a property of the schema rather than a habit of the caller.
**Read first:** `plan.md §5.6` (`signal_event`), `src/covenant_radar/ingestion/statements/` (the pattern to follow).
**Contracts:** `C-22` `POST /api/v1/ingest/signals`.
**Files owned:** `src/covenant_radar/domain/signals/__init__.py`, `taxonomy.py`, `src/covenant_radar/ingestion/signals/__init__.py`, `framework.py`, `src/covenant_radar/services/ingestion.py`, `src/covenant_radar/api/v1/routers/ingest.py` (signals), `tests/unit/test_signal_taxonomy.py`, `tests/integration/test_signal_ingestion.py`
**Behaviour:** a closed taxonomy of families and their event types with required payload fields per type; an ingestion pipeline that validates, computes the content hash from the event's natural key, inserts with conflict-ignore, and returns counts of inserted, duplicate and rejected.
**Every case:** an unknown family or event type → rejected into quarantine with the reason, the rest of the batch proceeding, because one bad row must not stop a day; a duplicate content hash → counted as duplicate, not an error; a payload missing a field its type requires → rejected naming the field; a batch failing mid-way → nothing committed; an event for an unknown borrower → quarantined, never auto-creating a borrower.
**Steps:** 1. Define the taxonomy with required payload fields per event type. 2. Implement content-hash computation from the natural key, documented so two sources agreeing on an event agree on its hash. 3. Implement the pipeline with conflict-ignore insertion and counts. 4. Implement quarantine for signals. 5. Expose the API ingest route.
**Tests:** `tests/unit/test_signal_taxonomy.py` — `test_six_families_closed`, `test_required_payload_fields_per_type`, `test_content_hash_stable_across_sources`; `tests/integration/test_signal_ingestion.py` — `test_batch_inserts_and_counts`, `test_duplicate_counted_not_errored`, `test_unknown_type_quarantined_rest_proceed`, `test_unknown_borrower_quarantined`, `test_mid_batch_failure_commits_nothing`.
**Run:** `pytest -q tests/unit/test_signal_taxonomy.py tests/integration/test_signal_ingestion.py` 8 passed.
**Done when:** the eight tests pass and the same batch ingested twice produces zero duplicates.
**Evidence:** an ingestion report for the reference portfolio's signal stream.

---

### [x] T-043 · Signal source adapters and the connector hand-off point
`Milestone: M-2` · `Builds: R-10` · `Days: 1.0` · `Depends on: T-042` · `Snapshot: var/snapshots/T-043/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the seam that `T-123`'s connector framework will plug into, defined now so the pipeline is not rebuilt around it later.
**Context:** signals arrive from files, from the API, from connectors and from feeds. Each source converts to the same event shape at its edge, so the pipeline has exactly one input format.
**Read first:** `src/covenant_radar/ingestion/signals/framework.py`, `plan.md §6` (`C-55`, `C-56`).
**Contracts:** the source interface `C-55` and `C-56` will satisfy.
**Files owned:** `src/covenant_radar/ingestion/signals/sources.py`, `file_source.py`, `api_source.py`, `tests/integration/test_signal_sources.py`
**Behaviour:** a `SignalSource` interface yielding validated events with their source reference; a file source for CSV and JSON with a mapping; an API source over the ingest payload; and a registry so a new source is a registration rather than a change to the pipeline.
**Every case:** a source yielding a malformed event → that event quarantined, the source continuing; a source raising mid-stream → nothing committed for that batch and the error reported with the source named; two sources yielding the same event → one insert, one duplicate; a source with no mapping configured → refused before reading anything.
**Steps:** 1. Define the `SignalSource` interface and the registry. 2. Implement the file source with mapping. 3. Implement the API source. 4. Wire both into the pipeline with per-source reporting.
**Tests:** `tests/integration/test_signal_sources.py` — `test_file_source_yields_validated_events`, `test_api_source_matches_file_source_shape`, `test_source_error_commits_nothing`, `test_two_sources_same_event_deduplicated`, `test_unmapped_source_refused`.
**Run:** `pytest -q tests/integration/test_signal_sources.py` 5 passed.
**Done when:** the five tests pass and adding a source requires no change to the pipeline.
**Evidence:** the source registry listing.

---

### [x] `T-044` · Idempotence, late arrival and watermarking
`Milestone: M-2` · `Builds: R-10` · `Days: 1.5` · `Depends on: T-042` · `Snapshot: var/snapshots/T-044/` · `Build: DEFERRED — out of scope for this window`

**Goal:** an event that arrives after its day has been scored is not dropped, and the days it affects are rescored, because in a real bank feed late data is normal and silent loss is not acceptable.
**Context:** `spec §R-10.c`. The processing watermark is per source; an event before it is stored, marked late, and triggers recomputation of the affected days.
**Read first:** `src/covenant_radar/ingestion/signals/framework.py`, `plan.md §5.6`.
**Contracts:** the recomputation queue `T-121` drains.
**Files owned:** `src/covenant_radar/ingestion/signals/watermark.py`, `src/covenant_radar/services/ingestion.py` (late handling), `tests/integration/test_late_arrival.py`
**Behaviour:** a per-source watermark advanced on successful ingestion; an event dated before it stored with `is_late` set and a recomputation request queued for the affected borrower and date range; the queue idempotent so ten late events for one borrower produce one recomputation.
**Every case:** an event exactly at the watermark → not late; one before it → late, stored, recomputation queued; ten late events for one borrower → one recomputation request covering the widest range; a late event for a borrower with no forecasts yet → stored, no recomputation queued, and the reason recorded; the watermark never moving backwards, even if a source replays an old file.
**Steps:** 1. Implement per-source watermark storage and advancement. 2. Implement late detection and marking. 3. Implement recomputation request coalescing by borrower and date range. 4. Guard the watermark against regression. 5. Record the no-forecast case explicitly.
**Tests:** `tests/integration/test_late_arrival.py` — `test_event_at_watermark_not_late`, `test_late_event_stored_and_marked`, `test_late_events_coalesce_to_one_request`, `test_no_forecast_records_reason`, `test_watermark_never_regresses`.
**Run:** `pytest -q tests/integration/test_late_arrival.py` 5 passed.
**Done when:** the five tests pass and no late event is ever dropped.
**Evidence:** a late-arrival trace showing coalescing.

---

### [x] T-045 · Ingestion reporting and quarantine for signals
`Milestone: M-2` · `Builds: R-10` · `Days: 0.5` · `Depends on: T-044` · `Snapshot: var/snapshots/T-045/` · `Build: DEFERRED — out of scope for this window`

**Goal:** every ingestion run leaves a report a data steward can act on, so quarantine depth is a metric rather than a mystery.
**Context:** `spec §R-10.d` and `spec §20`'s quarantine-depth alert.
**Read first:** `src/covenant_radar/services/ingestion.py`, `plan.md §5.3` (`quarantine_row`).
**Contracts:** the report `T-115`'s admin screen and `T-143`'s metrics read.
**Files owned:** `src/covenant_radar/services/ingestion.py` (reporting), `src/covenant_radar/db/repositories/ingestion.py`, `tests/integration/test_ingestion_report.py`
**Behaviour:** a per-run report with counts by outcome, per-family volumes, lag against the source's stated as-of date, and the top rejection reasons; quarantine rows resolvable by a steward with a reason.
**Every case:** a run with no rejects → the report still written, because absence of a report is indistinguishable from a run that did not happen; a rejection reason appearing more than the configured share of rows → the report flags it as a probable mapping error rather than listing five thousand identical failures; a quarantine row resolved → the report retained unchanged, since a report is a record of what happened, not of what was later fixed.
**Steps:** 1. Implement report assembly and persistence. 2. Implement the dominant-reason detection. 3. Implement quarantine resolution for signals. 4. Expose counts for metrics.
**Tests:** `tests/integration/test_ingestion_report.py` — `test_report_written_even_with_no_rejects`, `test_dominant_reason_flagged`, `test_resolution_does_not_alter_prior_report`, `test_counts_exposed_for_metrics`.
**Run:** `pytest -q tests/integration/test_ingestion_report.py` 4 passed.
**Done when:** the four tests pass and every run has a report.
**Evidence:** a sample report.

---

### [x] `T-046` · Evidence item model and derivation from events
`Milestone: M-2` · `Builds: R-11` · `Days: 1.5` · `Depends on: T-042` · `Snapshot: var/snapshots/T-046/` · `Build: #26 · Phase 3 · cum 12.5h`

**Goal:** raw events become typed evidence — the unit the rest of the product reasons about — with the identity rules that let an item persist, decay and be superseded rather than being recreated every day.
**Context:** `spec §R-11`. An evidence item is identified by borrower, facility, family and evidence type, so a run of payment delays is one item that lengthens rather than fourteen items that pile up. That identity decision is what makes persistence measurable at all.
**Read first:** `plan.md §5.6` (`evidence_item`, `evidence_transition`), `src/covenant_radar/domain/signals/taxonomy.py`.
**Contracts:** `C-34`'s `EvidenceScore` inputs.
**Files owned:** `src/covenant_radar/domain/signals/evidence.py`, `src/covenant_radar/db/repositories/evidence.py`, `tests/unit/test_evidence_model.py`
**Behaviour:** derivation from events to items with first-seen, last-seen, the contributing event ids and the window counts; an item never recreated once it exists, only extended, transitioned or superseded.
**Every case:** two events of the same type on the same day → one item, count incremented once per day not once per event, with the convention documented; an event for a type with no existing item → a new item at `transient`; an item with no event inside the decay horizon → retained, decaying, never deleted; an item whose facility is later deactivated → retained and marked, because the history is still evidence; the certificate-overdue item from `T-039` → derived here like any other family.
**Steps:** 1. Define the item identity and the derivation rules. 2. Implement derivation with day-level counting. 3. Implement the repository with no delete method. 4. Wire the certificate-overdue source. 5. Record every state change as a transition row.
**Tests:** `tests/unit/test_evidence_model.py` — `test_identity_groups_events_into_one_item`, `test_same_day_events_count_once`, `test_new_type_creates_transient_item`, `test_repository_has_no_delete`, `test_deactivated_facility_item_retained`, `test_certificate_overdue_derives_like_any_family`.
**Run:** `pytest -q tests/unit/test_evidence_model.py` 6 passed.
**Done when:** the six tests pass and no code path deletes an evidence item.
**Evidence:** the derivation trace for one borrower's payment stream.

---

### [x] `T-047` · Persistence scoring
`Milestone: M-2` · `Builds: R-11` · `Days: 1.5` · `Depends on: T-046, T-012` · `Snapshot: var/snapshots/T-047/` · `Build: #27 · Phase 3 · cum 12.9h`

**Goal:** the first half of the answer to `problem.md`'s core challenge — a rule that distinguishes a run from a blip, with its boundary tested at exactly its value.
**Context:** `spec §17.5` T3: sustained if at least fourteen consecutive days **or** at least three events in thirty days, inclusive at both. Read from the threshold store, never typed here.
**Read first:** `spec §17.5` (T3), `plan.md §6` (`C-34`), `src/covenant_radar/config/thresholds.py`.
**Contracts:** `C-34` `score_evidence` (the persistence portion).
**Files owned:** `src/covenant_radar/domain/signals/persistence.py`, `tests/unit/test_persistence.py`, `tests/property/test_persistence_invariants.py`
**Behaviour:** consecutive-day run length and rolling-window event count computed as of a date; the sustained decision from T3's two arms; the arm that fired recorded so the trace and the why-panel can name it.
**Every case:** exactly fourteen consecutive days → sustained; exactly three events in thirty days → sustained; thirteen days and two events → transient; a gap of one day inside a run → the run restarts, and the convention is documented and tested rather than assumed; an item whose events all fall outside the window → count zero and run zero; the arm that fired always recorded, because "sustained" without saying which rule fired is not an explanation.
**Steps:** 1. Implement run-length computation with the documented gap convention. 2. Implement the rolling-window count. 3. Implement the T3 decision reading both arms from the store. 4. Record the firing arm. 5. Write property tests for monotonicity in run length and count.
**Tests:** `tests/unit/test_persistence.py` — `test_exactly_fourteen_days_sustained`, `test_exactly_three_events_sustained`, `test_thirteen_days_two_events_transient`, `test_single_day_gap_restarts_run`, `test_firing_arm_recorded`, `test_thresholds_read_from_store_not_literal`; `tests/property/test_persistence_invariants.py` — `test_longer_run_never_less_sustained`, `test_more_events_never_less_sustained`.
**Run:** `pytest -q tests/unit/test_persistence.py tests/property/test_persistence_invariants.py` all passed.
**Done when:** both boundaries are tested at their exact values and the firing arm is always recorded.
**Evidence:** the boundary test output.

---

### [x] `T-048` · Materiality scoring
`Milestone: M-2` · `Builds: R-11` · `Days: 1.5` · `Depends on: T-047, T-034` · `Snapshot: var/snapshots/T-048/` · `Build: #28 · Phase 3 · cum 13.4h`

**Goal:** the second half of the noise answer — an item that persists but moves nothing is noted and excluded from pressure rather than escalated.
**Context:** `spec §17.5` T4: evidence counts if projected ninety-day headroom erosion is at least 5% of the threshold value, inclusive. This requires the engine's headroom, which is why it depends on `T-034`.
**Read first:** `spec §17.5` (T4), `src/covenant_radar/domain/covenants/headroom.py`, `domain/signals/persistence.py`.
**Contracts:** `C-34` `score_evidence` (the materiality portion).
**Files owned:** `src/covenant_radar/domain/signals/materiality.py`, `tests/unit/test_materiality.py`
**Behaviour:** projected erosion computed per item against each covenant it could affect; the item's materiality taken as the maximum across those covenants; `counts_toward_pressure` set from T4; the covenant that drove the maximum recorded.
**Every case:** materiality exactly at 5% → counts; below → the item stays visible on the ledger with `counts_toward_pressure` false and the reason recorded, never hidden; an item affecting no covenant → materiality zero and the reason recorded, not an exception; an item affecting several covenants → the maximum used and the driving covenant named; a covenant whose threshold is zero or absent → excluded from the maximum with the reason, never a division by zero.
**Steps:** 1. Implement the erosion projection per item per covenant. 2. Take the maximum and record the driving covenant. 3. Apply T4 from the store. 4. Handle the no-covenant and zero-threshold cases explicitly. 5. Test the boundary at exactly the value.
**Tests:** `tests/unit/test_materiality.py` — `test_exactly_five_percent_counts`, `test_below_threshold_visible_but_excluded_with_reason`, `test_no_affected_covenant_zero_with_reason`, `test_maximum_across_covenants_and_driver_named`, `test_zero_threshold_excluded_not_divided`, `test_threshold_read_from_store`.
**Run:** `pytest -q tests/unit/test_materiality.py` 6 passed.
**Done when:** the six tests pass and a sub-threshold item is still visible on the ledger.
**Evidence:** the boundary test output.

---

### [x] `T-049` · Decay and visibility
`Milestone: M-2` · `Builds: R-11` · `Days: 1.0` · `Depends on: T-047` · `Snapshot: var/snapshots/T-049/` · `Build: #29 · Phase 3 · cum 13.8h`

**Goal:** old evidence fades rather than either vanishing or lingering at full weight, and the fading is visible on screen so the desk can see why yesterday's alarm is quieter today.
**Context:** `spec §R-11.e`. Decay is geometric from last observation, floored at zero, and a decayed item is **still listed** with its decay state — the specification is explicit that it is never hidden and never deleted.
**Read first:** `spec §R-11.e`, `src/covenant_radar/domain/signals/persistence.py`, `src/covenant_radar/config/thresholds.py`.
**Contracts:** `C-34`'s `decay_factor`.
**Files owned:** `src/covenant_radar/domain/signals/decay.py`, `tests/unit/test_decay.py`, `tests/property/test_decay_invariants.py`
**Behaviour:** `decay_factor(days_since_last_seen, rate)` returning a value in zero to one; sustained items decaying only after their run breaks; the factor multiplying an item's contribution to forecast pressure and never its visibility.
**Every case:** zero days since last seen → factor one; a factor below the display floor → the item still returned and still rendered, with its state shown; a sustained item with a live run → no decay applied; a decayed item receiving a new event → the factor resets and the transition is recorded; the rate read from configuration, never a literal.
**Steps:** 1. Implement the decay function. 2. Apply it only to pressure, never to inclusion. 3. Implement reset on new observation with a transition row. 4. Write property tests for monotonicity and bounds.
**Tests:** `tests/unit/test_decay.py` — `test_zero_days_factor_one`, `test_decayed_item_still_returned`, `test_live_run_does_not_decay`, `test_new_event_resets_and_records_transition`, `test_rate_read_from_configuration`; `tests/property/test_decay_invariants.py` — `test_factor_within_zero_and_one`, `test_factor_monotonic_in_days`.
**Run:** `pytest -q tests/unit/test_decay.py tests/property/test_decay_invariants.py` all passed.
**Done when:** decay affects pressure only, and no decayed item is ever hidden.
**Evidence:** a decay curve for one item over thirty days.

---

### [x] `T-050` · Supersession and revision
`Milestone: M-2` · `Builds: R-11` · `Days: 1.5` · `Depends on: T-049` · `Snapshot: var/snapshots/T-050/` · `Build: #30 · Phase 3 · cum 14.3h`

**Goal:** when later evidence contradicts earlier evidence, the risk view revises and **both states remain reconstructable** — the property `problem.md` names and most alerting systems lack.
**Context:** `spec §R-11.c`. Nothing is deleted. A payment arriving after a delay supersedes the delay item; the delay item remains, marked, with its supersession link, and a reconstruction of yesterday's warning still shows what was true yesterday.
**Read first:** `spec §R-11.c`, `plan.md §5.6` (`superseded_by_id`, `supersedes_id`, `evidence_transition`).
**Contracts:** `C-34`'s supersession outputs.
**Files owned:** `src/covenant_radar/domain/signals/supersession.py`, `src/covenant_radar/services/ledger.py`, `tests/unit/test_supersession.py`, `tests/integration/test_revision.py`
**Behaviour:** contradiction rules per family declaring which event type supersedes which; supersession setting the link on both sides and writing a transition; a point-in-time read returning the state as of any past date.
**Every case:** a contradiction → both items retained with links, and a point-in-time read before the contradiction showing the prior state exactly; a contradiction arriving out of order → resolved by event date, not arrival order; a chain of supersessions → resolvable in both directions and terminating; an item superseded then contradicted again → a new item, not a resurrection of the first; a supersession rule that would delete → impossible, since the repository has no delete method.
**Steps:** 1. Declare contradiction rules per family as data. 2. Implement supersession with bidirectional links and transitions. 3. Implement the point-in-time read. 4. Handle out-of-order arrival by event date. 5. Test chains and repeated contradictions.
**Tests:** `tests/unit/test_supersession.py` — `test_contradiction_links_both_sides`, `test_out_of_order_resolved_by_event_date`, `test_chain_terminates_and_is_bidirectional`, `test_repeated_contradiction_creates_new_item`; `tests/integration/test_revision.py` — `test_point_in_time_read_returns_prior_state`, `test_risk_view_revises_after_contradiction`, `test_nothing_is_ever_deleted`.
**Run:** `pytest -q tests/unit/test_supersession.py tests/integration/test_revision.py` 7 passed.
**Done when:** the seven tests pass and a point-in-time read reproduces the state before any revision.
**Evidence:** a before-and-after reconstruction for one contradiction.

---

### [x] `T-051` · Stage-3 trace rows and ledger explainability
`Milestone: M-2` · `Builds: R-11` · `Days: 1.0` · `Depends on: T-050` · `Snapshot: var/snapshots/T-051/` · `Build: #31 · Phase 3 · cum 14.6h`

**Goal:** the ledger's decisions are as inspectable as the engine's — which rule fired, which threshold was compared, which side the value fell.
**Context:** `plan.md §8.6`. Stage 3 uses the shape `T-037` fixed. The why-panel's credibility rests on the code stages being openable, and this is one of them.
**Read first:** `src/covenant_radar/domain/trace.py`, `services/ledger.py`.
**Contracts:** `C-41`.
**Files owned:** `src/covenant_radar/services/ledger.py` (the trace write), `tests/integration/test_stage3_trace.py`
**Behaviour:** one stage-3 row per borrower per scoring run carrying every item's persistence and materiality inputs, the T3 arm that fired, the T4 comparison with its side, the decay factors and the supersessions applied.
**Every case:** an item that changed state → the transition and its cause in the row; an item that did not → present with its unchanged state, because absence from the trace reads as absence from the analysis; a borrower with no evidence → a row recorded stating so, not an omitted row; the rule version stamped so a change to the scoring rules is visible in every affected trace.
**Steps:** 1. Assemble the stage-3 inputs and outputs. 2. Build the thresholds-compared entries for T3 and T4 with sides. 3. Write the row per borrower per run with the rule version. 4. Handle the no-evidence case explicitly.
**Tests:** `tests/integration/test_stage3_trace.py` — `test_one_row_per_borrower_per_run`, `test_row_names_t3_arm_and_t4_side`, `test_unchanged_items_present`, `test_no_evidence_borrower_still_traced`, `test_rule_version_stamped`.
**Run:** `pytest -q tests/integration/test_stage3_trace.py` 5 passed.
**Done when:** the five tests pass and every scoring run traces every borrower.
**Evidence:** a sample stage-3 row.

---

### [x] `T-052` · Trend projection and the daily path
`Milestone: M-2` · `Builds: R-12` · `Days: 2.0` · `Depends on: T-034, T-050` · `Snapshot: var/snapshots/T-052/` · `Build: #32 · Phase 3 · cum 15.3h`

**Goal:** the trajectory itself — a transparent trend over recent periods adjusted by sustained-evidence pressure, walked day by day and stored, so the interface reads rather than re-models.
**Context:** `spec §R-12`. **Pure code, no model.** Storing the whole path is what makes `spec §18`'s 100 ms horizon step achievable and what guarantees the display can never produce a number the record does not hold.
**Read first:** `plan.md §6` (`C-35`), `src/covenant_radar/domain/covenants/evaluate.py`, `domain/signals/materiality.py`.
**Contracts:** `C-35` `project`.
**Files owned:** `src/covenant_radar/domain/forecast/__init__.py`, `trend.py`, `path.py`, `tests/unit/test_projection.py`, `tests/property/test_projection_invariants.py`
**Behaviour:** a least-squares slope over the configured number of recent complete periods converted to a per-day drift; a pressure term summing sustained items' materiality times decay times direction sign; a day-zero-to-horizon path; every term returned for the trace.
**Every case:** fewer than two usable observations → slope zero, the path flat, and the reason recorded rather than a number invented; an observation that is not computable → excluded and the exclusion recorded, never treated as zero; no sustained evidence → pressure zero and the projection is trend alone; a pressure term that would reverse the trend → applied as computed, because the data says what it says, with both terms visible in the trace; the path length always horizon plus one, including day zero.
**Steps:** 1. Implement the slope over complete observations with exclusion recording. 2. Convert to per-day drift with the documented period-length convention. 3. Implement the pressure term. 4. Walk the path and return it with every term. 5. Write property tests for path length, endpoint consistency and monotonicity under a constant drift.
**Tests:** `tests/unit/test_projection.py` — `test_slope_hand_worked`, `test_fewer_than_two_observations_flat_with_reason`, `test_not_computable_observation_excluded_and_recorded`, `test_no_sustained_evidence_is_trend_only`, `test_pressure_can_oppose_trend_and_both_visible`, `test_path_length_is_horizon_plus_one`; `tests/property/test_projection_invariants.py` — `test_constant_drift_monotonic_path`, `test_day_zero_equals_current_value`.
**Run:** `pytest -q tests/unit/test_projection.py tests/property/test_projection_invariants.py` all passed.
**Done when:** the hand-worked slope is exact and day zero always equals the current value.
**Evidence:** a projected path for the deteriorating cohort's hero borrower.

---

### [x] `T-053` · Threshold crossing and dating
`Milestone: M-2` · `Builds: R-12` · `Days: 1.5` · `Depends on: T-052` · `Snapshot: var/snapshots/T-053/` · `Build: #33 · Phase 3 · cum 15.8h`

**Goal:** the product's headline output — the **date** a specific covenant crosses its specific threshold.
**Context:** `spec §R-12.a` requires the dated crossing within ten days of the labelled breach on the deteriorating cohort. `spec §R-12.f`: a covenant already in breach crosses today, not never.
**Read first:** `src/covenant_radar/domain/forecast/path.py`, `domain/covenants/headroom.py`.
**Contracts:** `C-35`'s crossing output.
**Files owned:** `src/covenant_radar/domain/forecast/crossing.py`, `tests/unit/test_crossing.py`, `tests/integration/test_cohort_dating.py`
**Behaviour:** the first day the projected value meets or passes the threshold in the covenant's direction, converted to a date; none when it does not cross inside the horizon; the crossing day, the value at it and the margin returned.
**Every case:** already in breach at day zero → the crossing date is today, not none and not the past; a trajectory moving away from the threshold → none, with the direction recorded so the interface can say "improving" rather than "no data"; a trajectory touching the threshold exactly → a crossing on that day, matching the engine's boundary convention so two parts of the product never disagree about the same value; a threshold changed by an exception mid-horizon → the exception's threshold used from its effective date, with the change visible in the path; a flat trajectory already at the threshold → day zero.
**Steps:** 1. Implement first-crossing detection by direction. 2. Handle the already-breached and never-crossing cases explicitly. 3. Apply effective-dated threshold changes inside the horizon. 4. Align the boundary convention with the engine and assert it in a shared test. 5. Verify against the cohort labels.
**Tests:** `tests/unit/test_crossing.py` — `test_already_breached_crosses_today`, `test_improving_returns_none_with_direction`, `test_exact_touch_is_a_crossing`, `test_boundary_matches_engine_convention`, `test_mid_horizon_threshold_change_applied`; `tests/integration/test_cohort_dating.py` — `test_deteriorating_cohort_within_ten_days_of_labels`, `test_stable_cohort_never_crosses`.
**Run:** `pytest -q tests/unit/test_crossing.py tests/integration/test_cohort_dating.py` 7 passed.
**Done when:** the deteriorating cohort's dates are within tolerance of their labels and the boundary convention matches the engine exactly.
**Evidence:** the cohort dating report with predicted and labelled dates side by side.

---

### [x] `T-054` · Probability mapping, clamping and term capture
`Milestone: M-2` · `Builds: R-12` · `Days: 1.5` · `Depends on: T-053` · `Snapshot: var/snapshots/T-054/` · `Build: #34 · Phase 3 · cum 16.3h`

**Goal:** a probability per horizon from distance, velocity and pressure, through a function anyone can read, with every term stored so the why-panel shows arithmetic rather than an assertion.
**Context:** `spec §R-12` and `spec §22.4`, which names the calibration of this mapping as the product's weakest claim. That is precisely why the function is transparent, its weights are configuration, and every term is stored.
**Read first:** `plan.md §6` (`C-36`), `src/covenant_radar/config/thresholds.py`, `domain/forecast/crossing.py`.
**Contracts:** `C-36` `probability`.
**Files owned:** `src/covenant_radar/domain/forecast/probability.py`, `tests/unit/test_probability.py`, `tests/property/test_probability_invariants.py`
**Behaviour:** a documented monotone mapping over the three normalised inputs with weights read from configuration; the result clamped to a maximum below one so **no screen ever shows certainty**; every input, weight and intermediate returned for the trace.
**Every case:** a result that would exceed the clamp → clamped, and the clamping recorded rather than hidden; a covenant already in breach → probability at the clamp for every horizon, with the reason recorded; zero distance, zero velocity and zero pressure → the documented neutral value, not an arbitrary one; weights read from configuration and a test proving no weight literal exists in the module; a longer horizon never yielding a lower probability for the same inputs, proven by property test.
**Steps:** 1. Implement the normalisations for distance, velocity and pressure. 2. Implement the mapping with configured weights. 3. Apply the clamp and record it. 4. Return every term. 5. Write property tests for bounds and horizon monotonicity.
**Tests:** `tests/unit/test_probability.py` — `test_hand_worked_mapping`, `test_clamped_below_one_and_recorded`, `test_already_breached_at_clamp_with_reason`, `test_neutral_inputs_documented_value`, `test_no_weight_literal_in_module`; `tests/property/test_probability_invariants.py` — `test_within_bounds`, `test_monotonic_in_each_input`, `test_longer_horizon_never_lower`.
**Run:** `pytest -q tests/unit/test_probability.py tests/property/test_probability_invariants.py` all passed.
**Done when:** every property holds, the clamp is enforced, and no weight is a literal.
**Evidence:** a worked example with every term printed.

---

### [x] `T-055` · Confidence model from completeness, support and staleness
`Milestone: M-2` · `Builds: R-12` · `Days: 1.5` · `Depends on: T-054` · `Snapshot: var/snapshots/T-055/` · `Build: #35 · Phase 3 · cum 16.8h`

**Goal:** the number that decides whether a probability may be shown at all, so the product says "insufficient evidence — watching" instead of asserting a figure it cannot support.
**Context:** `spec §R-12.d` and T2. This is the render guard's input, and `spec §17.5` requires the suppression to hold **everywhere**, including the API and the digest, not only on the screen.
**Read first:** `plan.md §6` (`C-37`), `spec §17.5` (T2), `src/covenant_radar/domain/covenants/evaluate.py` (staleness).
**Contracts:** `C-37` `confidence`.
**Files owned:** `src/covenant_radar/domain/forecast/confidence.py`, `tests/unit/test_confidence.py`, `tests/property/test_confidence_invariants.py`
**Behaviour:** a product of data completeness, evidence support and a staleness factor, each documented and each returned; a `below_confidence_floor` flag set from T2; the dominant limiting factor recorded so the interface can say *why* confidence is low rather than only that it is.
**Every case:** confidence exactly at T2 → shown, because the floor is inclusive; zero complete periods → confidence zero, probability absent, and the reason recorded; a stale latest test → the staleness factor applied and named as the limiting factor; every factor at its maximum → confidence one; the limiting factor always identified, because "low confidence" without a cause is not actionable.
**Steps:** 1. Implement the three factors. 2. Implement the product and the flag from T2. 3. Identify and record the limiting factor. 4. Write property tests for bounds and monotonicity in each factor.
**Tests:** `tests/unit/test_confidence.py` — `test_exactly_at_t2_is_shown`, `test_zero_periods_zero_confidence_with_reason`, `test_stale_test_applies_factor_and_names_it`, `test_all_factors_maximum_gives_one`, `test_limiting_factor_always_recorded`; `tests/property/test_confidence_invariants.py` — `test_within_bounds`, `test_monotonic_in_each_factor`.
**Run:** `pytest -q tests/unit/test_confidence.py tests/property/test_confidence_invariants.py` all passed.
**Done when:** the inclusive boundary is tested at its exact value and the limiting factor is always recorded.
**Evidence:** a confidence breakdown for three borrowers with different limiting factors.

---

### [x] `T-056` · Forecast persistence, runs, versioning and staleness marking
`Milestone: M-2` · `Builds: R-12` · `Days: 1.5` · `Depends on: T-055` · `Snapshot: var/snapshots/T-056/` · `Build: #36 · Phase 3 · cum 17.3h`

**Goal:** every forecast belongs to a run, so a whole day's scoring is reproducible, and **any probability shown anywhere exists in a record first**.
**Context:** `plan.md §5.7`. The run carries the threshold snapshot and the rule versions in force, which is what lets an auditor reconstruct why an eighteen-month-old warning said what it said.
**Read first:** `plan.md §5.7`, `src/covenant_radar/domain/forecast/`, `config/thresholds.py`.
**Contracts:** `C-03`'s data source; `C-21`'s forecast resources.
**Files owned:** `src/covenant_radar/services/scoring.py`, `src/covenant_radar/db/repositories/forecast.py`, `tests/integration/test_forecast_persistence.py`
**Behaviour:** a run created per scoring pass carrying its date, the threshold snapshot and the rule versions; forecasts written per covenant version per horizon with the daily path; the run marked complete only when every covenant has been attempted; staleness days recorded from the data as-of date.
**Every case:** a run interrupted → left incomplete and resumable, never partially presented as a day's result; the same run re-executed → identical outputs, proven by content hash; a forecast for a covenant with no computable test → written with the reason, absent probability and zero confidence, never omitted, because an omitted covenant is one nobody looks at; a probability appearing on any surface without a matching record → a test failure; the horizon set read from configuration so a customer can add a horizon without code.
**Steps:** 1. Implement run creation with the snapshot and versions. 2. Implement per-covenant forecast writing with the path. 3. Implement completion and resumption. 4. Record staleness from the data as-of date. 5. Write a test that scans every surface for a probability with no backing record.
**Tests:** `tests/integration/test_forecast_persistence.py` — `test_run_carries_snapshot_and_versions`, `test_interrupted_run_incomplete_and_resumable`, `test_rerun_identical_by_content_hash`, `test_uncomputable_covenant_written_with_reason`, `test_no_probability_without_a_record`, `test_horizons_read_from_configuration`.
**Run:** `pytest -q tests/integration/test_forecast_persistence.py` 6 passed.
**Done when:** the six tests pass and no probability can be rendered that is not in a record.
**Evidence:** a run record with its snapshot reference.

---

### [x] `T-057` · Driver attribution and normalisation
`Milestone: M-2` · `Builds: R-13` · `Days: 1.5` · `Depends on: T-056` · `Snapshot: var/snapshots/T-057/` · `Build: #37 · Phase 3 · cum 17.8h`

**Goal:** the answer to "why", expressed as shares that sum to one, so a probability can be argued with instead of only believed.
**Context:** `spec §R-13` and T5: a contribution at or above ten per cent of the risk delta is listed with its share; the remainder folds into a single `other` row.
**Read first:** `plan.md §6` (`C-38`), `spec §17.5` (T5), `src/covenant_radar/domain/forecast/probability.py`.
**Contracts:** `C-38` `attribute`.
**Files owned:** `src/covenant_radar/domain/forecast/attribution.py`, `tests/unit/test_attribution.py`, `tests/property/test_attribution_invariants.py`
**Behaviour:** contributions computed per term — trend, each sustained evidence item, each data-quality factor — normalised to sum to one, with those at or above T5 listed individually and the remainder in one `other` row.
**Every case:** a contribution exactly at T5 → listed, not folded; every contribution below T5 → a single `other` row at one; a negative contribution, where a factor reduces risk → represented as negative and named, because hiding it would misstate the picture; all contributions zero → the documented neutral attribution with its reason, not a division by zero; the shares summing to one within floating tolerance in every case, proven by property test.
**Steps:** 1. Compute per-term contributions from the probability function's stored terms. 2. Normalise with the zero-total case handled explicitly. 3. Apply T5 and fold the remainder. 4. Preserve sign. 5. Write the summation property test.
**Tests:** `tests/unit/test_attribution.py` — `test_exactly_t5_is_listed`, `test_all_below_t5_single_other_row`, `test_negative_contribution_named`, `test_zero_total_documented_neutral`, `test_threshold_read_from_store`; `tests/property/test_attribution_invariants.py` — `test_shares_sum_to_one`, `test_listed_shares_never_below_t5`.
**Run:** `pytest -q tests/unit/test_attribution.py tests/property/test_attribution_invariants.py` all passed.
**Done when:** shares always sum to one and the T5 boundary is tested at its exact value.
**Evidence:** an attribution breakdown for the hero borrower.

---

### [x] `T-058` · Attribution links to evidence and stage-4 trace
`Milestone: M-2` · `Builds: R-13` · `Days: 1.0` · `Depends on: T-057` · `Snapshot: var/snapshots/T-058/` · `Build: #38 · Phase 3 · cum 18.1h`

**Goal:** a driver is clickable — it resolves to the evidence item behind it — and stage 4 writes the trace row that makes the whole forecast openable.
**Context:** `spec §R-13.c` and `plan.md §8.6`. Stage 4 shows trend slope, pressure terms, the mapping curve and the threshold comparison with its side, which is what turns a probability into something a credit officer can dispute specifically rather than generally.
**Read first:** `src/covenant_radar/domain/trace.py`, `domain/forecast/attribution.py`, `services/scoring.py`.
**Contracts:** `C-41`.
**Files owned:** `src/covenant_radar/services/scoring.py` (the driver and trace writes), `src/covenant_radar/db/repositories/driver.py`, `tests/integration/test_stage4_trace.py`
**Behaviour:** driver rows persisted with their evidence links; one stage-4 trace row per forecast carrying the slope, drift, pressure terms, mapping weights, confidence factors and the T1 and T2 comparisons with sides.
**Every case:** a driver with no evidence item, such as the trend term → persisted with a null link and a type that says so, never a broken reference; the `other` row → persisted and flagged, so the interface can render it without special-casing; a forecast below the confidence floor → the trace still written with the reason, because "why is there no number" is a question the panel must answer; the rule version stamped on every row.
**Steps:** 1. Persist driver rows with links and types. 2. Assemble the stage-4 trace inputs and outputs. 3. Build the thresholds-compared entries for T1 and T2 with sides. 4. Write the row for suppressed forecasts too. 5. Stamp the rule version.
**Tests:** `tests/integration/test_stage4_trace.py` — `test_driver_links_resolve_to_evidence`, `test_trend_driver_has_typed_null_link`, `test_other_row_flagged`, `test_suppressed_forecast_still_traced_with_reason`, `test_trace_names_t1_t2_with_sides`, `test_rule_version_stamped`.
**Run:** `pytest -q tests/integration/test_stage4_trace.py` 6 passed.
**Done when:** the six tests pass and every driver either links to evidence or declares why it does not.
**Evidence:** a stage-4 trace row with its threshold comparisons.

---

### [x] `T-059` · Urgency, banding and the deterministic tie-break
`Milestone: M-2` · `Builds: R-14` · `Days: 1.5` · `Depends on: T-056` · `Snapshot: var/snapshots/T-059/` · `Build: #39 · Phase 3 · cum 18.6h`

**Goal:** the ordered morning — borrowers ranked by urgency, banded by T1, with an ordering that is total and reproducible.
**Context:** `spec §R-14`. Urgency is probability times exposure times confidence at the worst covenant-horizon. `spec §R-14.b`: the tie-break is documented, applied and shown in the why-panel, because a queue whose order changes between two identical runs cannot be trusted.
**Read first:** `plan.md §6` (`C-39`), `spec §17.5` (T1), `src/covenant_radar/services/scoring.py`.
**Contracts:** `C-39` `rank`.
**Files owned:** `src/covenant_radar/domain/triage/__init__.py`, `urgency.py`, `banding.py`, `tests/unit/test_urgency.py`, `tests/property/test_ordering_invariants.py`
**Behaviour:** the worst covenant-horizon selected per borrower; urgency computed; the band from T1 with the boundary belonging to the higher band; a total ordering by urgency, then exposure, then reference, so no two runs disagree.
**Every case:** probability exactly at the act threshold → act band; exactly at amber → amber; identical urgency → larger exposure first, then reference ascending, and the applied rule recorded for the why-panel; a borrower with no forecast → included at the bottom with the reason, never omitted; a borrower whose only forecast is below the confidence floor → included with the suppressed state, banded as watch, and the reason recorded.
**Steps:** 1. Implement worst-horizon selection. 2. Implement urgency. 3. Implement banding with the documented boundary. 4. Implement the two-level tie-break and record the applied rule. 5. Write the property test for total ordering and run-to-run stability.
**Tests:** `tests/unit/test_urgency.py` — `test_boundary_probabilities_band_correctly`, `test_worst_horizon_selected`, `test_tie_break_exposure_then_reference`, `test_applied_tie_break_recorded`, `test_no_forecast_borrower_included_with_reason`, `test_suppressed_forecast_banded_watch_with_reason`; `tests/property/test_ordering_invariants.py` — `test_ordering_is_total`, `test_two_runs_identical_order`.
**Run:** `pytest -q tests/unit/test_urgency.py tests/property/test_ordering_invariants.py` all passed.
**Done when:** the ordering is total, stable across runs, and every boundary is tested at its exact value.
**Evidence:** the ranked queue for the reference portfolio.

---

### [x] `T-060` · What-changed computation between runs
`Milestone: M-2` · `Builds: R-14` · `Days: 1.0` · `Depends on: T-059` · `Snapshot: var/snapshots/T-060/` · `Build: #40 · Phase 3 · cum 18.9h`

**Goal:** the desk sees movement, not just position — what changed since the last run and why.
**Context:** `spec §R-14.d`. A queue that shows only today's state makes the reader re-derive yesterday's, which is the work the product exists to remove.
**Read first:** `src/covenant_radar/domain/triage/urgency.py`, `plan.md §5.7` (`triage_entry`).
**Contracts:** `C-22`'s `what_changed`.
**Files owned:** `src/covenant_radar/domain/triage/changes.py`, `src/covenant_radar/services/triage.py`, `tests/unit/test_what_changed.py`
**Behaviour:** a comparison against the prior completed run producing a typed change — new to act, band improved or worsened, probability moved by a stated amount, newly suppressed, newly unsuppressed, no change — with the driver of the largest movement named where one dominates.
**Every case:** no prior run → the documented first-run state, not "no change", because the two are different facts; a borrower absent from the prior run → newly monitored; a borrower absent from this run but present before → surfaced as newly unmonitored rather than silently vanishing; a movement below the reporting threshold → no change, with the threshold configured; the dominant driver named only when one exceeds the configured share, and otherwise omitted rather than guessed.
**Steps:** 1. Implement prior-run lookup, skipping incomplete runs. 2. Implement the typed change computation. 3. Implement dominant-driver identification with its share rule. 4. Handle first run, appearance and disappearance explicitly. 5. Persist onto the triage entry.
**Tests:** `tests/unit/test_what_changed.py` — `test_first_run_state_is_distinct_from_no_change`, `test_band_worsened_named`, `test_new_borrower_marked_newly_monitored`, `test_disappeared_borrower_surfaced`, `test_movement_below_reporting_threshold_is_no_change`, `test_dominant_driver_named_only_when_dominant`.
**Run:** `pytest -q tests/unit/test_what_changed.py` 6 passed.
**Done when:** the six tests pass and a borrower can never silently disappear from the queue.
**Evidence:** a what-changed listing across two consecutive runs.

---

### [x] `T-061` · Queue query, filtering and the saved-view model
`Milestone: M-2` · `Builds: R-14` · `Days: 1.5` · `Depends on: T-060, T-016` · `Snapshot: var/snapshots/T-061/` · `Build: #41 · Phase 3 · cum 19.4h`

**Goal:** the queue read path — scoped, filtered, sorted and paginated inside `spec §18`'s budget on a portfolio of ten thousand rows.
**Context:** `spec §R-22.d`. This is a read-performance task as much as a feature task: the queue is opened first every morning by every user, and it must be fast on the largest supported portfolio.
**Read first:** `src/covenant_radar/db/scoping.py`, `plan.md §5.7` (`triage_entry` indexes), `spec §18`.
**Contracts:** `C-01`'s data source; `C-21`'s queue resource.
**Files owned:** `src/covenant_radar/db/repositories/triage.py`, `src/covenant_radar/services/triage.py` (query), `src/covenant_radar/domain/triage/views.py`, `tests/integration/test_queue_query.py`, `tests/perf/test_queue_performance.py`
**Behaviour:** a single query returning the latest complete run's entries for the caller's scope, filtered by band, portfolio, industry, assignee, SMA band and case state, sorted by rank, with cursor pagination; a saved-view model storing a named filter set.
**Every case:** no complete run yet → the documented empty state with its reason, not an error; a filter naming a value that does not exist → an empty result, not a failure; a cursor from a superseded run → refused with a message telling the caller to reload, rather than returning a mixture of two runs; scope applied in the query, proven by the leakage test; ten thousand rows returning the first page inside the latency budget, proven by the performance test.
**Steps:** 1. Implement the repository query with scope, filters, ordering and cursor pagination. 2. Add the covering index and verify the plan uses it. 3. Implement the saved-view model. 4. Handle the no-run and stale-cursor cases. 5. Write the performance test at the reference size.
**Tests:** `tests/integration/test_queue_query.py` — `test_latest_complete_run_only`, `test_every_filter_applies`, `test_stale_cursor_refused_with_reload`, `test_scope_applied_in_query`, `test_saved_view_round_trips`, `test_no_run_returns_documented_empty_state`; `tests/perf/test_queue_performance.py` — `test_first_page_within_budget_at_reference_size`.
**Run:** `pytest -q tests/integration/test_queue_query.py` 6 passed · `pytest -q tests/perf/test_queue_performance.py` within budget.
**Done when:** the query is inside budget at the reference size and the plan uses the intended index.
**Evidence:** the query plan, the timing.

---

### [x] `T-062` · Intervention effect models and applicability rules
`Milestone: M-2` · `Builds: R-15` · `Days: 1.5` · `Depends on: T-052` · `Snapshot: var/snapshots/T-062/` · `Build: #52 · Phase 5 · cum 24.7h`

**Goal:** each intervention's effect expressed as a parameterised transform on a trajectory, with the assumptions it rests on stated as data — because a counterfactual without its assumptions is a guess with a chart.
**Context:** `spec §R-16.c`: an action the simulator cannot model cannot be recommended, so the effect model is a required field on a catalogue entry rather than an optional one.
**Read first:** `plan.md §6` (`C-40`), `src/covenant_radar/domain/forecast/path.py`, `plan.md §5.7` (`intervention`).
**Contracts:** `C-40`'s intervention facts.
**Files owned:** `src/covenant_radar/domain/interventions/__init__.py`, `effects.py`, `applicability.py`, `tests/unit/test_effect_models.py`
**Behaviour:** a closed set of effect model types — level shift on an input line, rate change on the drift, threshold relaxation, pressure reduction, and a combination — each parameterised, each declaring its assumptions and its applicable covenant classes.
**Every case:** an effect applied to a covenant class it does not fit → refused naming why, never silently producing no change, because "no change" and "not applicable" are different answers; a parameter outside its declared range → refused naming the range; an effect whose assumptions are empty → refused at registration, since every counterfactual rests on something; a combination effect → applies its components in a documented order and states all their assumptions.
**Steps:** 1. Define the effect model types and their parameters. 2. Implement each as a pure transform on the projection inputs. 3. Implement applicability by covenant class. 4. Require and validate the assumption list. 5. Define the combination ordering.
**Tests:** `tests/unit/test_effect_models.py` — `test_each_effect_transforms_as_documented`, `test_inapplicable_class_refused_naming_why`, `test_parameter_out_of_range_refused`, `test_empty_assumptions_refused`, `test_combination_order_documented_and_applied`.
**Run:** `pytest -q tests/unit/test_effect_models.py` 5 passed.
**Done when:** every effect states its assumptions and no inapplicable effect returns silently.
**Evidence:** the effect model catalogue with assumptions.

---

### [x] `T-063` · Counterfactual simulation and multi-option comparison
`Milestone: M-2` · `Builds: R-15` · `Days: 2.0` · `Depends on: T-062` · `Snapshot: var/snapshots/T-063/` · `Build: #53 · Phase 5 · cum 25.4h`

**Goal:** the risk officer's real question answered — what does each option buy, against doing nothing.
**Context:** `spec §R-15`. Up to four interventions compared side by side against the baseline, each with its own crossing date, probability and assumptions. The deltas must reconcile exactly to a recomputation, because a simulator that cannot be checked is a toy.
**Read first:** `plan.md §6` (`C-40`), `src/covenant_radar/domain/forecast/`, `domain/interventions/effects.py`.
**Contracts:** `C-40` `simulate`, `C-11` `POST /simulations`.
**Files owned:** `src/covenant_radar/domain/interventions/simulate.py`, `src/covenant_radar/services/simulation.py`, `tests/unit/test_simulation.py`, `tests/integration/test_simulation_comparison.py`
**Behaviour:** an intervention applied to the stored projection inputs, the path recomputed, the crossing redated, the probability remapped, and the deltas against the baseline returned with the full assumption list; comparison of up to four plus the baseline in one result.
**Every case:** an intervention that moves the crossing beyond the horizon → the crossing reported as none within the horizon and the delta expressed as at-least, not as a fabricated date; an intervention with no effect on this covenant → a zero delta, explicitly, distinct from inapplicability; more than four compared → refused naming the limit, because a comparison nobody can read is not a comparison; an intervention applied to a covenant already breached → simulated on the cure path with the assumption stated; the same simulation run twice → identical, proven by content hash.
**Steps:** 1. Implement application of an effect to the projection inputs. 2. Recompute path, crossing and probability through the same functions the baseline used, never a parallel implementation. 3. Compute deltas and assemble assumptions. 4. Implement comparison with the limit. 5. Assert reconciliation against an independent recomputation.
**Tests:** `tests/unit/test_simulation.py` — `test_delta_reconciles_to_recomputation`, `test_beyond_horizon_reported_as_at_least`, `test_zero_effect_distinct_from_inapplicable`, `test_breached_covenant_simulates_cure_path_with_assumption`, `test_identical_reruns`; `tests/integration/test_simulation_comparison.py` — `test_four_options_plus_baseline`, `test_more_than_four_refused`, `test_uses_same_functions_as_baseline`.
**Run:** `pytest -q tests/unit/test_simulation.py tests/integration/test_simulation_comparison.py` 8 passed.
**Done when:** every delta reconciles to an independent recomputation and no parallel forecast implementation exists.
**Evidence:** a four-option comparison for the hero borrower.

---

### [x] `T-064` · Simulation persistence and assumption capture
`Milestone: M-2` · `Builds: R-15` · `Days: 1.0` · `Depends on: T-063` · `Snapshot: var/snapshots/T-064/` · `Build: #54 · Phase 5 · cum 25.7h`

**Goal:** a simulation a memo cites must still be retrievable when the memo is reopened, with the assumptions it rested on.
**Context:** `spec §R-15.d`. This is why a simulation is a persisted artefact rather than transient screen state.
**Read first:** `plan.md §5.7` (`simulation`), `src/covenant_radar/services/simulation.py`.
**Contracts:** `C-11`; `C-60`.
**Files owned:** `src/covenant_radar/services/simulation.py` (persistence), `src/covenant_radar/db/repositories/simulation.py`, `tests/integration/test_simulation_persistence.py`
**Behaviour:** every simulation persisted with its forecast, intervention, parameters, assumptions, result and creator; retrievable by id indefinitely; superseded rather than overwritten when re-run with the same parameters against a newer forecast.
**Every case:** a simulation whose forecast run has been superseded → still retrievable, marked as based on a superseded run, because that is what the memo cited; a re-run with identical parameters → a new record linked to the prior, both retained; a retired intervention → its simulations still resolve; a simulation referenced by a memo → cannot be purged before the memo's retention expires, enforced by the retention job's reference check.
**Steps:** 1. Persist with the full parameter and assumption set. 2. Implement retrieval and supersession linking. 3. Mark simulations based on superseded runs. 4. Register the retention reference. 5. Audit creation.
**Tests:** `tests/integration/test_simulation_persistence.py` — `test_retrievable_after_run_superseded_and_marked`, `test_rerun_links_not_overwrites`, `test_retired_intervention_still_resolves`, `test_memo_referenced_simulation_not_purgeable`, `test_creation_audited`.
**Run:** `pytest -q tests/integration/test_simulation_persistence.py` 5 passed.
**Done when:** the five tests pass and no memo can reference a simulation that later disappears.
**Evidence:** a retrieved simulation with its assumptions.

---

### [x] `T-065` · Threshold and weight calibration on the reference portfolio
`Milestone: M-2` · `Supports: spec [OPEN-06]` · `Days: 3.0` · `Depends on: T-063, T-041` · `Snapshot: var/snapshots/T-065/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** the shipped default thresholds and weights are set by running the procedure once against labelled data, so they are defaults with a provenance rather than numbers someone liked.
**Context:** `spec §17.5` says the defaults are engineering starting values calibrated on the reference portfolio, and `spec §22.4` says an uncalibrated probability must not be presented as a measured one. `spec §6`'s G1 and G3 pull in opposite directions on purpose, so neither may be tuned alone. **This task changes no code**: a calibration that needs a code change is a defect report.
**Read first:** `spec §6` (G1, G3), `spec §17.5`, `evaluation/reference_portfolio/labels.py`, `config/thresholds.default.json`.
**Contracts:** `C-18`; `C-79`'s scoring output.
**Files owned:** `config/thresholds.default.json`, `docs/calibration/reference-portfolio.md`, `tests/integration/test_calibrated_cohorts.py`
**Behaviour:** an iterative procedure over T1, T3, T4, T5 and the mapping weights, scored against G1 (lead time achieved on the deteriorating cohort) and G3 (escalation share and false-escalation rate on the noisy and stable cohorts) jointly, with every step recorded.
**Every case:** a setting that improves G1 by worsening G3 past its target → rejected and the rejection recorded, because the pair is the point; a value that would violate a threshold's own invariant → refused by the store; no setting satisfying both → stop at the closest, publish the miss honestly in the calibration record, and do not adjust a target to meet it; the calibration re-run from the recorded settings → reproducing the recorded scores exactly.
**Steps:** 1. Score the reference portfolio at the current defaults and record the baseline. 2. Adjust at most two values per iteration, re-scoring after each. 3. Stop when both goals are met or three consecutive iterations improve neither. 4. Write the calibration record: every value before and after, every iteration, the final scores, and any miss. 5. Add a regression test that the calibrated cohorts still behave, so a later change that breaks calibration fails the build.
**Tests:** `tests/integration/test_calibrated_cohorts.py` — `test_deteriorating_cohort_lead_time_meets_g1`, `test_noisy_cohort_escalation_within_g3`, `test_stable_cohort_below_amber`, `test_amber_share_within_g3_on_every_day`, `test_calibration_reproducible_from_record`.
**Run:** `pytest -q tests/integration/test_calibrated_cohorts.py` 5 passed · the calibration record written.
**Done when:** both goals are met, or the closest setting is in force with the miss published. **The Phase 3 gate can now be demonstrated.**
**Evidence:** the calibration record, the cohort scores before and after, the M-2 gate record.

---

### M-3 · Interface and explainability — 29.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-066` · Audit store, hash chain and append-only enforcement
`Milestone: M-3` · `Builds: R-20` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-066/` · `Build: #42 · Phase 4 · cum 19.9h`

**Goal:** the compliance artefact the whole product is defended by — an event store nothing can edit, whose tampering is detectable rather than merely forbidden.
**Context:** `spec §R-20.b`. Append-only by grant and by code is defeated by anyone with database access; the hash chain is what turns "must not be changed" into "cannot be changed without it showing". `plan.md §5.9` fixes the hash input.
**Read first:** `plan.md §5.9` (`audit_event`), `plan.md §6` (`C-60`), `src/covenant_radar/core/ids.py`.
**Contracts:** `C-60` `audit.record`.
**Files owned:** `src/covenant_radar/audit/__init__.py`, `record.py`, `store.py`, `chain.py`, `src/covenant_radar/db/repositories/audit.py`, `tests/unit/test_audit_chain.py`, `tests/integration/test_audit_store.py`
**Behaviour:** `record(...)` is the only write path; the sequence is monotonic; each row's hash covers its own content and the previous row's hash; `verify_chain(from, to)` returns the first break with both rows named; the application's database role holds no update or delete grant on the table.
**Every case:** a payload carrying a personal-class value directly → refused by the payload validator, which requires a reference instead, because an audit trail must be readable by people who may not read the underlying data; a non-serialisable payload → `TypeError` naming the field; a row inserted with a wrong previous hash → refused; a deliberately corrupted row → detected by verification, naming the sequence; concurrent writes → sequence and chain both correct, proven under concurrency; any update or delete statement against the table anywhere in the source → a failing test.
**Steps:** 1. Implement canonical payload serialisation so the same content always hashes the same. 2. Implement the chained insert inside a transaction that serialises on the sequence. 3. Implement verification with break reporting. 4. Implement the payload validator rejecting personal-class values. 5. Write the source scan and the grant assertion.
**Tests:** `tests/unit/test_audit_chain.py` — `test_hash_covers_content_and_previous`, `test_canonical_serialisation_stable`, `test_wrong_previous_hash_refused`, `test_verification_names_first_break`; `tests/integration/test_audit_store.py` — `test_personal_value_in_payload_refused`, `test_non_serialisable_raises_naming_field`, `test_concurrent_writes_keep_chain_valid`, `test_no_update_or_delete_in_source`, `test_application_role_lacks_grants`.
**Run:** `pytest -q tests/unit/test_audit_chain.py tests/integration/test_audit_store.py` 9 passed.
**Done when:** the nine tests pass, the chain survives concurrency, and a corrupted row is detected.
**Evidence:** a verification report, the grant listing.

---

### [x] `T-067` · Audit emission across every service, with the coverage test
`Milestone: M-3` · `Builds: R-20` · `Days: 1.5` · `Depends on: T-066` · `Snapshot: var/snapshots/T-067/` · `Build: #43 · Phase 4 · cum 20.3h`

**Goal:** every state-changing operation writes an event, and the ones that deliberately do not are enumerated rather than forgotten.
**Context:** `plan.md §3.3`: services emit audit events; routes and repositories never do. A coverage test enumerates every state-changing service method and asserts each either records an event or appears on an explicit exemption list with a reason.
**Read first:** `src/covenant_radar/audit/record.py`, every module under `src/covenant_radar/services/`.
**Contracts:** `C-60`.
**Files owned:** `src/covenant_radar/audit/events.py` (the event-type enumeration), `src/covenant_radar/services/*` (emission only), `tests/security/test_audit_coverage.py`
**Behaviour:** an enumerated event type per operation; emission inside the same transaction as the change, so an event and its change are never separated; an exemption registry with a reason per entry.
**Every case:** a service method that changes state and records nothing and is not exempt → the coverage test fails naming it; an event written outside the changing transaction → a test failure, because a committed change with an uncommitted event is an unrecorded change; a read of personal-class data by a privileged role → recorded as an access event with its purpose; a bulk operation → one event per affected entity plus one summary event, so neither the detail nor the shape is lost; an exemption with no reason → refused.
**Steps:** 1. Enumerate the event types. 2. Add emission to every state-changing service method. 3. Add access-purpose logging for privileged personal-class reads. 4. Build the exemption registry with reasons. 5. Write the coverage test by introspection so a new service method is covered automatically.
**Tests:** `tests/security/test_audit_coverage.py` — `test_every_state_changing_method_records_or_is_exempt`, `test_event_written_in_same_transaction`, `test_privileged_personal_read_logged_with_purpose`, `test_bulk_writes_detail_and_summary`, `test_exemption_requires_reason`.
**Run:** `pytest -q tests/security/test_audit_coverage.py` 5 passed.
**Done when:** the five tests pass and the exemption list is short enough to read and every entry justified.
**Evidence:** the coverage report listing every method and its event type.

---

### [x] `T-068` · Warning reconstruction assembly
`Milestone: M-3` · `Builds: R-20` · `Days: 1.5` · `Depends on: T-067, T-058` · `Snapshot: var/snapshots/T-068/` · `Build: #44 · Phase 4 · cum 20.7h`

**Goal:** any warning, however old, rebuilt end to end from one call — the question every inspector actually asks.
**Context:** `spec §R-20.a`: source data with provenance, covenant version, calculation, trend, evidence in force, thresholds in force, forecast, memo, overrides and dispositions, all reachable from one view. `spec §R-20.d`: where retention has purged an input, say so and name the rule rather than fabricating the gap.
**Read first:** `plan.md §5` (every table involved), `src/covenant_radar/audit/store.py`, `db/repositories/trace.py`.
**Contracts:** `C-15` `GET /audit/warnings/{id}`.
**Files owned:** `src/covenant_radar/audit/reconstruct.py`, `src/covenant_radar/services/reconstruction.py`, `tests/integration/test_reconstruction.py`
**Behaviour:** `reconstruct(forecast_id)` assembles every part as of the forecast's own run, using the threshold snapshot and covenant version in force then, not now; each part carrying its provenance; purged parts represented explicitly.
**Every case:** a threshold changed since the warning → the snapshot in force at the time used, and a test proves reconstruction is unaffected by later changes; a covenant amended since → the version used then is shown; evidence superseded since → the state as of then is shown, with the later supersession noted separately; a purged source → named with its retention rule and the purge date; a warning with no memo → that part marked not generated rather than omitted; reconstruction of the same warning twice → identical.
**Steps:** 1. Resolve the run, snapshot and versions as of the forecast. 2. Assemble each part with point-in-time reads. 3. Represent purged parts explicitly. 4. Attach provenance to source data. 5. Assert stability against later changes.
**Tests:** `tests/integration/test_reconstruction.py` — `test_all_parts_present_from_one_call`, `test_uses_threshold_snapshot_in_force_then`, `test_uses_covenant_version_in_force_then`, `test_evidence_state_as_of_then`, `test_purged_source_named_with_rule`, `test_missing_memo_marked_not_generated`, `test_reconstruction_stable_after_later_changes`.
**Run:** `pytest -q tests/integration/test_reconstruction.py` 7 passed.
**Done when:** the seven tests pass and a later threshold change cannot alter a past reconstruction.
**Evidence:** a reconstruction before and after a threshold change, shown identical.

---

### [x] `T-069` · Evidence bundle export, manifest and verification
`Milestone: M-3` · `Builds: R-20` · `Days: 1.5` · `Depends on: T-068` · `Snapshot: var/snapshots/T-069/` · `Build: #65 · Phase 6 · cum 31.4h`

**Goal:** the inspector takes the trail away with them, and can prove it was not altered afterwards.
**Context:** `spec §R-20.c`: a manifest hash matching the contents, with every referenced document included.
**Read first:** `src/covenant_radar/audit/reconstruct.py`, `plan.md §5.4` (documents), `security/crypto.py`.
**Contracts:** `C-16` bundle export.
**Files owned:** `src/covenant_radar/audit/bundle.py`, `src/covenant_radar/services/reconstruction.py` (export), `tests/integration/test_evidence_bundle.py`
**Behaviour:** an archive containing the reconstruction as JSON, a human-readable PDF rendering, every referenced source document, the audit chain segment with its verification result, and a manifest listing every file with its hash and the manifest's own hash.
**Every case:** a bundle verified after a file is altered → verification fails naming the file; a referenced document missing from storage → the bundle records the absence with the reason and still verifies, because an honest gap is not corruption; a bundle for a warning whose chain segment fails verification → produced with the failure stated prominently, never silently omitted; a very large bundle → produced asynchronously with notification; the export itself → an audit event naming who exported what.
**Steps:** 1. Assemble the reconstruction, the PDF rendering and the documents. 2. Include and verify the chain segment. 3. Build the manifest with per-file and overall hashes. 4. Implement asynchronous production for large bundles. 5. Implement a verification command usable outside the product.
**Tests:** `tests/integration/test_evidence_bundle.py` — `test_manifest_hash_matches_contents`, `test_altered_file_fails_verification_naming_it`, `test_missing_document_recorded_and_still_verifies`, `test_chain_failure_stated_prominently`, `test_large_bundle_async_with_notification`, `test_export_audited`.
**Run:** `pytest -q tests/integration/test_evidence_bundle.py` 6 passed · `radarctl` bundle verification on a produced bundle exits 0.
**Done when:** the six tests pass and a bundle verifies outside the application.
**Evidence:** a produced bundle and its verification output.

---

### [x] `T-070` · Trace model, the unified stage record and its reader
`Milestone: M-3` · `Builds: R-21` · `Days: 1.5` · `Depends on: T-066` · `Snapshot: var/snapshots/T-070/` · `Build: #45 · Phase 4 · cum 21.1h`

**Goal:** the reader that turns stage rows into the why-panel's content, in one shape for code, model and future statistical stages.
**Context:** `plan.md §8.6` and `spec §17.6`. Stages 2, 3 and 4 already write rows (`T-037`, `T-051`, `T-058`); stages 1, 5, 6 and 7 will. The reader must present all of them uniformly, including the ones that did not run.
**Read first:** `src/covenant_radar/domain/trace.py`, `db/repositories/trace.py`, `spec §17.6`.
**Contracts:** `C-41`; `C-10`'s data source.
**Files owned:** `src/covenant_radar/audit/trace_reader.py`, `src/covenant_radar/services/explain.py`, `tests/unit/test_trace_reader.py`
**Behaviour:** `explain(subject)` returns every stage in order with its inputs, outputs, decider, deciding version, thresholds compared with sides, confidence, sources and a not-run marker where applicable; stage names resolved from one place so no template hardcodes them.
**Every case:** a subject with no rows at all → every stage returned as not-run, never an empty result; two rows for one stage → the later shown and the earlier retrievable, because nothing in the audit path is overwritten; a stage whose decider is a model → the prompt version and the code verdict present; a threshold entry missing a side → impossible, since the write path refuses it, and a reader test asserts the invariant holds on stored data; a subject of an unknown type → refused naming the valid types.
**Steps:** 1. Implement the reader with ordering and not-run padding. 2. Resolve stage names from the domain, not from templates. 3. Present model-stage fields uniformly with code-stage fields. 4. Assert the side invariant over stored rows. 5. Validate the subject type.
**Tests:** `tests/unit/test_trace_reader.py` — `test_all_stages_returned_in_order`, `test_no_rows_returns_all_not_run`, `test_later_row_shown_earlier_retrievable`, `test_model_stage_fields_present`, `test_stored_rows_all_carry_sides`, `test_unknown_subject_type_refused`.
**Run:** `pytest -q tests/unit/test_trace_reader.py` 6 passed.
**Done when:** the six tests pass and no template needs to know a stage's name.
**Evidence:** an explain output for a forecast covering every stage.

---

### [x] `T-071` · Why-panel rendering for code, model and statistical stages
`Milestone: M-3` · `Builds: R-21` · `Days: 2.0` · `Depends on: T-070, T-021` · `Snapshot: var/snapshots/T-071/` · `Build: #46 · Phase 4 · cum 21.6h`

**Goal:** the panel that makes the product's central claim checkable — every stage openable, including the ones no model touched.
**Context:** `spec §R-21.a` and `spec §17.6`. A code stage shows the rule and the numbers it compared, naming the threshold and which side the value fell. A model stage shows the masked prompt version, what came back and the code verdict. `spec §15.3`: the drawer is the one element in the design that casts a shadow.
**Read first:** `src/covenant_radar/services/explain.py`, `web/templates/_components/why_section.html`, `spec §17.6`.
**Contracts:** `C-10` `GET /why/...`.
**Files owned:** `src/covenant_radar/web/routes/why.py`, `web/templates/screens/why/*`, `web/static/css/why.css`, `tests/integration/test_why_panel.py`, `tests/a11y/test_why_panel_a11y.py`
**Behaviour:** a drawer opening from any shown decision, one collapsible section per stage in order, each showing what it received, what it produced, who decided, the deciding version, a threshold table with name, value, observed and side, the confidence and links to the source records.
**Every case:** a stage that did not run → its section says so and shows nothing else, never fabricated content; a threshold table row → always four populated cells, because a side-less comparison cannot be written; a model stage → prompt version, returned text and code verdict, with the model's text visually distinguished from computed figures; a suppressed forecast → the panel explains the suppression and names the limiting confidence factor; reduced motion → the drawer appears without sliding; the panel opened on a subject the caller may not see → `404`.
**Steps:** 1. Write the route resolving the subject and calling `explain`. 2. Write the section template driven entirely by the record. 3. Write the threshold table with the four columns. 4. Style with `why.css` using only tokens and the single drawer shadow. 5. Distinguish model-written text visibly. 6. Wire the escape-to-close and focus-return behaviour.
**Tests:** `tests/integration/test_why_panel.py` — `test_every_stage_section_present_in_order`, `test_not_run_section_shows_only_that`, `test_threshold_table_shows_value_observed_and_side`, `test_model_stage_shows_prompt_version_and_verdict`, `test_suppressed_forecast_explained_with_limiting_factor`, `test_out_of_scope_subject_404`; `tests/a11y/test_why_panel_a11y.py` — `test_axe_clean_both_themes`, `test_escape_closes_and_returns_focus`.
**Run:** `pytest -q tests/integration/test_why_panel.py tests/a11y/test_why_panel_a11y.py` 8 passed.
**Done when:** every stage opens, every threshold shows its side, and the drawer is the only shadow in the design.
**Evidence:** why-panel screenshots in both themes.

---

### [x] `T-072` · Why-panel API and the no-JavaScript full page
`Milestone: M-3` · `Builds: R-21` · `Days: 1.0` · `Depends on: T-071` · `Snapshot: var/snapshots/T-072/` · `Build: #70 · Phase 6 · cum 33.1h`

**Goal:** explainability that does not depend on script or on a browser — the same content as a full page and as JSON.
**Context:** `spec §R-21.d`. An explanation available only inside a drawer inside a working front end is an explanation an auditor cannot script, cite or archive.
**Read first:** `src/covenant_radar/web/routes/why.py`, `api/v1/routers/`.
**Contracts:** `C-10`; `C-21`'s explanation resource.
**Files owned:** `src/covenant_radar/web/routes/why.py` (full-page mode), `src/covenant_radar/api/v1/routers/explain.py`, `api/v1/schemas/explain.py`, `tests/integration/test_why_standalone.py`, `tests/contract/test_explain_contract.py`
**Behaviour:** the same URL renders a drawer fragment for an HTMX request and a full page for a direct one; the API returns the same content as JSON under the same permission and scope rules.
**Every case:** JavaScript disabled → the full page renders with identical content, proven by comparing extracted text; the API and the page disagreeing on any field → a contract test failure; a scoped caller → the same `404` on both surfaces; the JSON shape matching its schema exactly, proven by the contract test.
**Steps:** 1. Add fragment-versus-page negotiation on the existing route. 2. Write the API router and schema from the same service call. 3. Write the equivalence test comparing page text to JSON content. 4. Add the contract test.
**Tests:** `tests/integration/test_why_standalone.py` — `test_full_page_matches_drawer_content`, `test_no_javascript_renders_everything`, `test_api_matches_page`, `test_scope_enforced_on_both`; `tests/contract/test_explain_contract.py` — `test_schema_matches_implementation`.
**Run:** `pytest -q tests/integration/test_why_standalone.py tests/contract/test_explain_contract.py` 5 passed.
**Done when:** the five tests pass and the three surfaces cannot disagree.
**Evidence:** the equivalence-test output.

---

### [x] `T-073` · Portfolio queue screen
`Milestone: M-3` · `Builds: R-22` · `Days: 2.0` · `Depends on: T-061, T-021` · `Snapshot: var/snapshots/T-073/` · `Build: #47 · Phase 4 · cum 22.1h`

**Goal:** the screen the desk opens first — ranked rows, one click into a case file, and nothing on it that is not a reason to open something.
**Context:** `spec §15.4`: the queue is organised around *choosing whom to open*. No KPI tiles above it. The accent appears only in the band chips. Real content lengths throughout.
**Read first:** `src/covenant_radar/services/triage.py`, `web/templates/_components/`, `spec §15.4`.
**Contracts:** `C-01` `GET /`.
**Files owned:** `src/covenant_radar/web/routes/queue.py`, `web/templates/screens/queue/*`, `web/view_models/queue.py`, `tests/integration/test_queue_screen.py`, `tests/e2e/test_queue_flow.py`
**Behaviour:** ranked rows showing borrower, exposure, worst covenant, dated risk, band, SMA band, assignee, case state and what-changed; every row a single link target; all four state families designed.
**Every case:** empty scope → the designed empty state naming the next action, not a blank table; no completed run → the documented state saying so rather than an error; a borrower with a suppressed forecast → the row rendering the suppression text in place of a probability, never a bare number; a long entity name → wrapping, never truncated; the accent appearing anywhere but a band chip → a failing test; keyboard navigation reaching every row and opening one with Enter.
**Steps:** 1. Write the view model from the queue query. 2. Write the screen using only components. 3. Render band chips as the sole accent. 4. Wire the four states. 5. Make rows single link targets with a 32px minimum height. 6. Write the end-to-end flow test.
**Tests:** `tests/integration/test_queue_screen.py` — `test_order_matches_service_exactly`, `test_suppressed_row_shows_text_not_number`, `test_empty_scope_designed_state`, `test_no_completed_run_state`, `test_accent_only_in_band_chips`, `test_long_name_wraps`; `tests/e2e/test_queue_flow.py` — `test_keyboard_reaches_and_opens_a_row`, `test_renders_both_themes`.
**Run:** `pytest -q tests/integration/test_queue_screen.py tests/e2e/test_queue_flow.py` 8 passed · screenshots at three viewports in both themes.
**Done when:** the eight tests pass and a human has reviewed the screen against `spec §15.4`'s composition rules.
**Evidence:** the screenshots, the review note.

---

### [x] `T-074` · Queue filters, saved views and bulk selection
`Milestone: M-3` · `Builds: R-22` · `Days: 1.0` · `Depends on: T-073` · `Snapshot: var/snapshots/T-074/` · `Build: #69 · Phase 6 · cum 32.8h`

**Goal:** a desk can carve the queue into the slice it owns and keep that slice, without losing it on every visit.
**Context:** `spec §R-22.b` and `R-33`. Selection state here is what `T-139`'s bulk operations act on.
**Read first:** `src/covenant_radar/domain/triage/views.py`, `web/routes/queue.py`.
**Contracts:** `C-01`'s filter parameters.
**Files owned:** `src/covenant_radar/web/routes/queue.py` (filters), `web/templates/screens/queue/_filters.html`, `_selection.html`, `web/static/js/queue.js`, `tests/integration/test_queue_filters.py`
**Behaviour:** filters for band, portfolio, industry, assignee, SMA band and case state; a saved view storing a named filter set per user with an option to share; multi-select with a persistent count and a clear action.
**Every case:** a filter combination with no results → the designed empty state naming which filters are active and offering to clear them, not a blank table; a saved view referencing a portfolio the user has lost access to → the view loads with that filter dropped and the user told; a shared view opened by a user with a narrower scope → applies within their scope only; selection surviving pagination within a page set, and cleared explicitly on filter change with the user told; filters reflected in the URL so a view is linkable.
**Steps:** 1. Implement filter parsing and validation. 2. Implement saved views with sharing and the lost-access behaviour. 3. Implement selection with the count and the clear action. 4. Reflect filters in the URL. 5. Write the progressive-enhancement path so filters work without JavaScript.
**Tests:** `tests/integration/test_queue_filters.py` — `test_every_filter_applies`, `test_no_results_state_names_active_filters`, `test_saved_view_drops_lost_scope_and_tells_user`, `test_shared_view_applies_within_viewer_scope`, `test_selection_cleared_on_filter_change_with_notice`, `test_filters_in_url`, `test_filters_work_without_javascript`.
**Run:** `pytest -q tests/integration/test_queue_filters.py` 7 passed.
**Done when:** the seven tests pass and every filter works with script disabled.
**Evidence:** the test output, a shared-view scope demonstration.

---

### [x] `T-075` · Case file: layout, header facts, covenant strip
`Milestone: M-3` · `Builds: R-23` · `Days: 2.0` · `Depends on: T-073` · `Snapshot: var/snapshots/T-075/` · `Build: #48 · Phase 4 · cum 22.6h`

**Goal:** the screen that carries the product — one borrower being weighed, with exactly four facts above the fold and every covenant's position in a ledger.
**Context:** `spec §15.4`: the header holds exactly four facts and 40% whitespace. Every number is right-aligned on the decimal in the data face. `spec §R-23.b`: a borrower with no evidence and one covenant renders designed empty states with no blank panels and no console errors.
**Read first:** `spec §15.4`, `src/covenant_radar/services/`, `web/templates/_components/`.
**Contracts:** `C-02` `GET /borrowers/{ref}`.
**Files owned:** `src/covenant_radar/web/routes/borrower.py`, `web/templates/screens/borrower/index.html`, `_header.html`, `_covenants.html`, `web/view_models/borrower.py`, `tests/integration/test_case_file.py`, `tests/e2e/test_case_file_render.py`
**Behaviour:** a header with name, exposure, worst covenant and dated risk; a covenant strip as a ledger table with value, threshold in force, headroom, verdict, next test date and a trajectory arrow; every figure loaded from a record, none computed in the view.
**Every case:** a stale covenant → the row states the last complete period and the confidence reduction, not a dash; a not-computable covenant → the enumerated reason, not a dash; a borrower with one covenant and no evidence → designed empty states in every panel, no blank regions, no console errors; a header fact absent → the panel says which and why, never renders an empty slot; more than four facts appearing in the header → a failing test, because the density rule is a build rule.
**Steps:** 1. Write the view model assembling every panel's data from records. 2. Write the header with exactly four facts and the whitespace ratio. 3. Write the covenant strip as a ledger table with decimal alignment. 4. Wire empty, loading, error and degraded states per panel. 5. Assert in a test that the view computes no figure.
**Tests:** `tests/integration/test_case_file.py` — `test_header_holds_exactly_four_facts`, `test_covenant_rows_show_value_threshold_headroom_verdict`, `test_stale_row_states_last_period`, `test_not_computable_row_shows_reason`, `test_view_computes_no_figure`, `test_unknown_borrower_404`; `tests/e2e/test_case_file_render.py` — `test_empty_borrower_no_blank_panels_no_console_errors`, `test_renders_both_themes_three_viewports`.
**Run:** `pytest -q tests/integration/test_case_file.py tests/e2e/test_case_file_render.py` 8 passed · screenshots at three viewports in both themes.
**Done when:** the eight tests pass, no figure is computed in the view, and a human has reviewed the screen against `spec §15.4`.
**Evidence:** the screenshots, the review note.

---

### [x] `T-076` · Forecast panel and inline SVG trajectories
`Milestone: M-3` · `Builds: R-23` · `Days: 1.5` · `Depends on: T-075, T-056` · `Snapshot: var/snapshots/T-076/` · `Build: #49 · Phase 4 · cum 23.0h`

**Goal:** the trajectory made visible, always beside the figures it plots, with the confidence guard enforced at the point of render.
**Context:** `spec §15.2` forbid 3: no chart without its ledger line. `spec §R-12.d`: below the confidence floor, the words replace the number — here and on every other surface.
**Read first:** `src/covenant_radar/web/view_models/borrower.py`, `plan.md §5.7` (`forecast_path`), `spec §15.2`.
**Contracts:** `C-02`; `C-03`'s payload shape.
**Files owned:** `src/covenant_radar/web/templates/screens/borrower/_forecast.html`, `web/svg/trajectory.py`, `web/static/css/forecast.css`, `tests/integration/test_forecast_panel.py`
**Behaviour:** per covenant, the three named horizons with probability, confidence and dated crossing, alongside a hand-drawn inline SVG trajectory with the threshold line and the crossing tick; the ledger figures beside the figure, always.
**Every case:** confidence below the floor → the suppression text with its limiting factor in place of the probability, and the trajectory still drawn because headroom is still a fact; no crossing inside the horizon → the panel says so with the direction, not an empty date; a trajectory rendered without its ledger figures → a failing test; the SVG carrying a text equivalent, which is the ledger line itself; a covenant with no forecast → the panel states why.
**Steps:** 1. Write the SVG generator with the threshold line, the path and the crossing tick. 2. Write the panel template pairing each figure with its ledger row. 3. Enforce the confidence guard in the view model, not the template, so every surface inherits it. 4. Provide the text equivalent from the ledger. 5. Assert the pairing rule in a test.
**Tests:** `tests/integration/test_forecast_panel.py` — `test_three_horizons_rendered`, `test_below_floor_shows_text_and_limiting_factor`, `test_no_crossing_states_direction`, `test_trajectory_requires_ledger_figures`, `test_svg_has_text_equivalent`, `test_no_forecast_panel_states_why`.
**Run:** `pytest -q tests/integration/test_forecast_panel.py` 6 passed.
**Done when:** the six tests pass and no trajectory can render without its figures.
**Evidence:** panel screenshots including a suppressed case.

---

### [x] `T-077` · Horizon control: interaction, keyboard, reduced motion, stops fallback
`Milestone: M-3` · `Builds: R-23` · `Days: 2.0` · `Depends on: T-076` · `Snapshot: var/snapshots/T-077/` · `Build: #50 · Phase 4 · cum 23.5h`

**Goal:** the signature interaction — moving time forward and watching headroom deplete, the crossing tick appear with its date, the drivers ink into the margin and the header rewrite live.
**Context:** `spec §15.5`. It **reads stored daily paths and never re-models**, which is what makes the 100 ms step budget achievable and what guarantees it cannot produce a number the record does not hold. Direct manipulation, no tween: the display *is* the data at the selected day.
**Read first:** `spec §15.5`, `spec §18` (the step budget), `plan.md §6` (`C-03`), `web/templates/screens/borrower/_forecast.html`.
**Contracts:** `C-03` `GET /api/v1/forecasts/{ref}/path`.
**Files owned:** `src/covenant_radar/web/static/js/horizon.js`, `web/static/css/horizon.css`, `web/templates/screens/borrower/_horizon.html`, `src/covenant_radar/api/v1/routers/forecast.py`, `tests/integration/test_horizon_api.py`, `tests/e2e/test_horizon_control.py`, `tests/perf/test_horizon_step.py`
**Behaviour:** a range control bound to the panel; on input, one in-flight request for the selected day; covenant lines, probability, crossing tick, date and lit drivers updated from the response only; arrows step a day, shift-arrows a week, Home and End jump; the named horizons are tab stops; reduced motion and no-JavaScript both degrade to those stops as plain links.
**Every case:** a day outside the range → clamped client-side and refused server-side, so neither end trusts the other; a covenant that never crosses → no tick and the header stating no projected crossing; a suppressed forecast → the words, never a number, on this surface too; a response slower than the budget → the previous value held with no intermediate flicker; script disabled → the stops render as links carrying the day parameter and the page still works; reduced motion → discrete stops only.
**Steps:** 1. Implement the API route reading `forecast_path` with no recomputation and a test asserting no write occurs during a request. 2. Implement the control with single-flight fetching and coalescing. 3. Update every dependent element from the response only. 4. Implement the keyboard model and tab stops. 5. Implement the reduced-motion and no-JavaScript stop mode as a class swap over the same markup. 6. Add the step-timing performance test.
**Tests:** `tests/integration/test_horizon_api.py` — `test_payload_has_every_field`, `test_day_out_of_range_422`, `test_unknown_covenant_404`, `test_no_write_occurs_during_request`, `test_suppressed_forecast_returns_text_not_number`; `tests/e2e/test_horizon_control.py` — `test_tick_appears_with_date_at_crossing`, `test_keyboard_model_complete`, `test_reduced_motion_uses_stops`, `test_no_javascript_stops_are_links`; `tests/perf/test_horizon_step.py` — `test_step_within_budget`.
**Run:** `pytest -q tests/integration/test_horizon_api.py tests/e2e/test_horizon_control.py` 9 passed · `pytest -q tests/perf/test_horizon_step.py` within budget.
**Done when:** the step is inside budget, the control is fully keyboard-operable, and it degrades to stops in both fallback conditions.
**Evidence:** the timing, a recorded interaction, screenshots at three days.

---

### [x] `T-078` · Evidence margin, document strip and case actions
`Milestone: M-3` · `Builds: R-23` · `Days: 1.5` · `Depends on: T-075, T-051` · `Snapshot: var/snapshots/T-078/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the claim wearing its evidence — every driver and every covenant movement traceable, on the same screen, to the items behind it.
**Context:** `spec §15.1`: every claim wearing its evidence. `spec §R-11.e`: a decayed item is still listed with its state, never hidden.
**Read first:** `src/covenant_radar/services/ledger.py`, `web/view_models/borrower.py`, `spec §R-11`.
**Contracts:** `C-02`.
**Files owned:** `src/covenant_radar/web/templates/screens/borrower/_evidence.html`, `_documents.html`, `_actions.html`, `web/view_models/borrower.py` (extension), `tests/integration/test_evidence_margin.py`
**Behaviour:** the evidence ledger grouped by family, each item showing type, first and last seen, persistence, materiality, decay state, whether it counts toward pressure and its supersession links; the document and certificate strip; and the action row with why, memo, simulate and log-action.
**Every case:** a decayed item → listed with its decay state and greyed, never hidden; a superseded item → listed with a link to what superseded it, and the superseding item linking back; an item that does not count toward pressure → shown with the reason, so a user is not left wondering why a visible item changed nothing; a borrower with no documents → the strip states so and offers upload to those permitted; an action the role may not take → the control is not rendered, matching the authorization matrix exactly.
**Steps:** 1. Extend the view model with the ledger, documents and permitted actions. 2. Write the evidence margin grouped by family with every state visible. 3. Render supersession links in both directions. 4. Write the document and certificate strip. 5. Render the action row from the caller's permissions.
**Tests:** `tests/integration/test_evidence_margin.py` — `test_decayed_item_listed_with_state`, `test_supersession_links_both_directions`, `test_non_counting_item_shows_reason`, `test_no_documents_states_and_offers_upload`, `test_actions_match_permission_matrix`, `test_grouping_by_family`.
**Run:** `pytest -q tests/integration/test_evidence_margin.py` 6 passed.
**Done when:** the six tests pass and no evidence item is ever hidden from the ledger.
**Evidence:** case-file screenshots with a decayed and a superseded item visible.

---

### [x] `T-079` · Simulator screen and comparison view
`Milestone: M-3` · `Builds: R-25` · `Days: 1.5` · `Depends on: T-064, T-076` · `Snapshot: var/snapshots/T-079/` · `Build: #67 · Phase 6 · cum 32.2h`

**Goal:** options compared against doing nothing, with every assumption on screen rather than in a footnote.
**Context:** `spec §F-04`. The comparison is the deliverable: a single simulation without the baseline beside it invites the reader to imagine the counterfactual, which is the error the screen exists to prevent.
**Read first:** `src/covenant_radar/services/simulation.py`, `web/templates/screens/borrower/_forecast.html`.
**Contracts:** `C-11` `POST /simulations`.
**Files owned:** `src/covenant_radar/web/routes/simulator.py`, `web/templates/screens/simulator/*`, `web/view_models/simulation.py`, `tests/integration/test_simulator_screen.py`
**Behaviour:** intervention selection filtered to the applicable ones with parameters; up to four compared against the baseline in one table with crossing date, probability by horizon and the delta for each; every assumption listed under its option; a selection carried into memo generation.
**Every case:** an inapplicable intervention → not offered, and if forced by a direct request refused with the reason; a fifth option → refused naming the limit; an option with no effect → shown with a zero delta and the reason, distinct from being inapplicable; the baseline always present in the comparison, never optional; assumptions never collapsed behind a control that could be left closed.
**Steps:** 1. Write the route and the applicable-intervention query. 2. Write the parameter form per effect model. 3. Write the comparison table with the baseline column always first. 4. Render assumptions inline under each option. 5. Carry the selection into memo generation.
**Tests:** `tests/integration/test_simulator_screen.py` — `test_only_applicable_offered`, `test_forced_inapplicable_refused_with_reason`, `test_fifth_option_refused`, `test_zero_effect_distinct_from_inapplicable`, `test_baseline_always_present`, `test_assumptions_rendered_inline`.
**Run:** `pytest -q tests/integration/test_simulator_screen.py` 6 passed.
**Done when:** the six tests pass and no comparison can render without the baseline.
**Evidence:** a four-option comparison screenshot.

---

### [x] `T-080` · Audit search and reconstruction screens
`Milestone: M-3` · `Builds: R-25` · `Days: 1.5` · `Depends on: T-069` · `Snapshot: var/snapshots/T-080/` · `Build: #66 · Phase 6 · cum 31.8h`

**Goal:** the auditor's own screens — find any event, open any warning, take the bundle away.
**Context:** `spec §F-05`. The auditor is read-only everywhere and the interface must make that obvious rather than presenting controls that then refuse.
**Read first:** `src/covenant_radar/services/reconstruction.py`, `audit/store.py`, `web/templates/_components/`.
**Contracts:** `C-15`, `C-16`.
**Files owned:** `src/covenant_radar/web/routes/audit.py`, `web/templates/screens/audit/*`, `web/view_models/audit.py`, `tests/integration/test_audit_screens.py`
**Behaviour:** event search by actor, subject, type and date range with export; a reconstruction timeline showing every part in order with its provenance; a chain-verification indicator; and bundle export.
**Every case:** an auditor viewing any screen → no state-changing control rendered anywhere; a search returning more than the page size → cursor pagination with a stable order; a reconstruction with a purged part → the part named with its retention rule; a chain verification failure → shown prominently at the top, not as a subtle badge; an export → audited with the filter and the row count.
**Steps:** 1. Write the search route with filters, ordering and export. 2. Write the reconstruction timeline. 3. Surface chain verification prominently. 4. Wire bundle export with asynchronous production. 5. Ensure no state-changing control renders for a read-only role.
**Tests:** `tests/integration/test_audit_screens.py` — `test_no_state_changing_control_for_auditor`, `test_search_filters_and_stable_pagination`, `test_timeline_shows_every_part_in_order`, `test_purged_part_named_with_rule`, `test_chain_failure_shown_prominently`, `test_export_audited_with_filter_and_count`.
**Run:** `pytest -q tests/integration/test_audit_screens.py` 6 passed.
**Done when:** the six tests pass and a read-only role sees no control it cannot use.
**Evidence:** the screenshots, an export audit event.

---

### [x] `T-081` · Governance screens: thresholds, model registry, scoreboard
`Milestone: M-3` · `Builds: R-25` · `Days: 1.5` · `Depends on: T-080, T-012` · `Snapshot: var/snapshots/T-081/` · `Build: 2026-09-01 · offline session, no CI pipeline number assigned · verified with pytest`

**Goal:** the oversight surface a risk head is accountable for — what the thresholds are, who changed them, which models are approved, and how the product scores against its own baseline.
**Context:** `spec §R-25` and the FREE-AI expectation of proportionate oversight with named accountability. The scoreboard reads the stored evaluation run, so it is the same number the release notes carry.
**Read first:** `src/covenant_radar/config/thresholds.py`, `plan.md §5.9` (`model_registration`, `evaluation_run`).
**Contracts:** `C-18`; the registry and evaluation resources.
**Files owned:** `src/covenant_radar/web/routes/governance.py`, `web/templates/screens/governance/*`, `web/view_models/governance.py`, `tests/integration/test_governance_screens.py`
**Behaviour:** a threshold view showing current values, their boundary behaviour, the change history with actor and approver, and the proposal form for those permitted; a model registry view with component, provider, version, owner, approval and drift; and an evaluation scoreboard showing both arms against the pass marks per release.
**Every case:** a pending threshold change → shown as pending with the proposer named, and the approve control rendered only for someone who may approve and is not the proposer; a threshold with no change history → shown as at its shipped default with the calibration record referenced; no evaluation run recorded → the documented empty state, never a blank or a fabricated score; a drift breach → shown with its metric, window and the rollback state; a model without an approval record → flagged, because an unapproved model in use is the finding.
**Steps:** 1. Write the threshold view with boundary behaviour and history. 2. Write the proposal and approval controls under their permissions. 3. Write the model registry view with drift. 4. Write the scoreboard from the stored run. 5. Flag unapproved models and drift breaches prominently.
**Tests:** `tests/integration/test_governance_screens.py` — `test_pending_change_shows_proposer_and_hides_self_approval`, `test_default_threshold_references_calibration_record`, `test_no_evaluation_run_empty_state`, `test_drift_breach_shows_metric_window_and_rollback`, `test_unapproved_model_flagged`, `test_scoreboard_shows_both_arms`.
**Run:** `pytest -q tests/integration/test_governance_screens.py` 6 passed. Full-suite regression check (`pytest -q tests/integration tests/unit/test_thresholds.py tests/unit/test_model_domain.py tests/security`): 446 passed; the remaining 4 failures/2 errors were inspected by traceback and are unrelated to this task's files (two need a live PostgreSQL service this environment doesn't have; one fails inside `exports/memo.html` before the template loader ever reaches `governance/index.html`; one is `test_route_declarations.py`'s own `install_route_declaration_check` swallowing its `RouteDeclarationError` instead of raising it; one is `test_single_call_site.py` flagging `web/application.py`, a file this task never touched).
**Done when:** the six tests pass and an unapproved model in use is visible on the screen.
**Evidence:** the test output above. No browser screenshot was captured — Playwright's Chromium binary is not installed in this offline session (`playwright install` needs network access this corporate laptop does not have configured); the propose/approve HTTP flow was additionally smoke-tested end to end (propose → 403 for a non-approver → 303 approve by a distinct actor → snapshot updated → self-approval refused 409).

---

### [x] `T-082` · Dark theme completion and print styles
`Milestone: M-3` · `Builds: R-36` · `Days: 1.5` · `Depends on: T-078` · `Snapshot: var/snapshots/T-082/` · `Build: 2026-09-01 · offline session, no CI pipeline number assigned · verified with pytest`

**Goal:** both themes complete across every screen, chosen by system preference with a persisted override and no flash of the wrong one, plus print styles for the documents that leave the product.
**Context:** `spec §R-36`. The dark palette is derived by role, not inverted, and both themes clear the same contrast floors — which is why the check runs over both.
**Read first:** `src/covenant_radar/web/static/css/tokens.css`, every screen template, `spec §15.3`.
**Contracts:** none.
**Files owned:** `src/covenant_radar/web/static/css/print.css`, `web/static/css/*` (theme completion), `web/routes/preferences.py`, `tests/e2e/test_theme.py`, `tests/a11y/test_contrast_both_themes.py`
**Behaviour:** the theme resolved server-side from the user preference or the system hint before first paint; a toggle persisting the preference; a high-contrast variant; print styles for memos, reconstructions and reports.
**Every case:** no stored preference → the system hint used and no flash, proven by an end-to-end test that fails on a paint of the wrong theme; a screen with a token missing a dark value → the contrast check fails naming the token; a print render → legible in monochrome with no background fills and every link's target printed; a theme toggle without JavaScript → a form post that persists and re-renders; an accent chip in dark mode → still meeting its floor.
**Steps:** 1. Complete the dark values for every token in use. 2. Resolve the theme server-side and set the attribute on the root element. 3. Implement the toggle with a no-JavaScript fallback. 4. Write print styles. 5. Extend the contrast check over both themes and add it to the gate.
**Tests:** `tests/e2e/test_theme.py` — `test_no_flash_of_wrong_theme`, `test_no_stored_preference_uses_the_system_hint`, `test_toggle_persists_across_sessions`, `test_toggle_works_without_javascript`, `test_print_render_legible_monochrome`; `tests/a11y/test_contrast_both_themes.py` — `test_every_pair_meets_floor_in_both_themes`, `test_accent_chips_meet_their_floor_in_dark_mode`, `test_a_token_missing_its_dark_value_fails_naming_it`, `test_high_contrast_variant_widens_the_margin_in_both_themes`.
**Run:** `pytest -q tests/e2e/test_theme.py tests/a11y/test_contrast_both_themes.py` 9 passed · `pytest -q tests/unit/test_tokens.py` 7 passed. `python scripts/check_contrast.py --both-themes`: both palettes' token contrast passes (`validate_token_css` — which the a11y suite above already exercises directly — completes before the script's separate, repo-wide design-literal sweep runs); that sweep itself now exits 1, but solely on 17 pre-existing `admin.css` literals (lines 15, 83, 208, 239, 248, 291, 294, 309, 310, 340, 362, 363, 439, 491, 508, 514, 542) belonging to an unrelated, already-broken in-progress admin/users screen this task never touches — `tokens.css`, `app.css` and `print.css` contribute zero literals. Two pre-existing literals inside this task's own file scope (`app.css:1116` `2px`, `app.css:1271`'s `var(--radius, 4px)` fallback) were fixed in passing since they blocked this same gate. A pre-existing, repo-wide regex bug in `src/covenant_radar/i18n/__init__.py`'s `_TRANSLATION_CALL` (matching bare `tr(` with no word boundary, so Jinja's `selectattr('is_active')` false-positived as a translation call) was crashing `create_app()` — and therefore nearly every integration/e2e/a11y test in the repo — at import time; fixed with a one-character `\b` anchor, verified against `tests/integration/test_governance_screens.py` and a full non-`tests/property` run (995 passed, same pre-existing unrelated failures as before: `test_admin_users.py`, `test_horizon_control.py::test_no_javascript_stops_are_links`, `test_hardening.py::test_no_template_references_external_origin`, the PostgreSQL-only and `hypothesis`-only tests, `test_gate.py`, `test_single_call_site.py`, `test_route_declarations.py::test_missing_declaration_refuses_startup`, `tests/migration`).
**Done when:** both themes clear their floors on every screen and no flash occurs.
**Evidence:** the contrast report for both themes (`tests/a11y/test_contrast_both_themes.py`), print output (`src/covenant_radar/web/static/css/print.css`, proven by `test_print_render_legible_monochrome`). No browser screenshot was captured — same offline constraint noted on `T-081` (no network access to install Playwright's Chromium binary on this corporate laptop).

---

### T-083 · Automated accessibility audit and remediation
`Milestone: M-3` · `Builds: N-07` · `Days: 2.0` · `Depends on: T-082` · `Snapshot: var/snapshots/T-083/` · `Build: DEFERRED — out of scope for this window`

**Goal:** WCAG 2.2 AA proven on every screen, in both themes, as a gate rather than a report.
**Context:** `spec §N-07`. Accessibility built at the end is accessibility retrofitted; the components have carried it since `T-021`, and this task closes the gaps and locks the gate.
**Read first:** `spec §15.6`, every screen template, `web/templates/_components/`.
**Contracts:** none.
**Files owned:** `tests/a11y/*`, `src/covenant_radar/web/templates/**` (remediation only), `docs/accessibility.md`
**Behaviour:** an automated audit over every screen in every state in both themes; a keyboard walkthrough test for the primary flow; a zoom and reflow test at 200% and 320px; and a documented manual screen-reader procedure for the pre-release audit.
**Every case:** any violation on any screen → the gate fails naming the rule, the element and the screen; a screen reachable only in a state the audit does not cover → the coverage test fails, because an unaudited state is an unaudited screen; the primary flow not completable by keyboard → failure; content lost or overlapping at 320px or 200% zoom → failure; a remediation that changes a design token → refused, since tokens are `T-020`'s and a token change is a design decision.
**Steps:** 1. Enumerate every screen and state and assert the audit covers all of them. 2. Run the audit in both themes and fix every violation. 3. Write the keyboard walkthrough for the primary flow. 4. Write the zoom and reflow tests. 5. Document the manual screen-reader procedure. 6. Add all of it to the gate.
**Tests:** `tests/a11y/test_all_screens.py` — `test_every_screen_and_state_covered`, `test_zero_violations_both_themes`; `tests/a11y/test_keyboard.py` — `test_primary_flow_by_keyboard_only`, `test_focus_visible_everywhere`, `test_focus_order_matches_reading_order`; `tests/a11y/test_reflow.py` — `test_200_percent_zoom_no_loss`, `test_320px_no_horizontal_scroll`.
**Run:** `pytest -q tests/a11y` all passed · the gate includes the accessibility step.
**Done when:** zero violations across every screen and state in both themes, and the primary flow completes by keyboard alone. **M-3's gate can now be demonstrated.**
**Evidence:** the audit report, the keyboard walkthrough recording, the M-3 gate record.

---

### M-4 · Documents, intake and memo — 39.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-084` · Document model, upload, virus scan, encrypted store
`Milestone: M-4` · `Builds: R-04` · `Days: 2.0` · `Depends on: T-017, T-019` · `Snapshot: var/snapshots/T-084/` · `Build: #17 · Phase 2 · cum 7.3h`

**Goal:** the sanction letters and certificates the product reads about live somewhere safe, validated before they are stored, encrypted at rest, and behind a port so the storage backend is a deployment choice.
**Context:** `spec §R-04.d`. A file that fails the virus scan is never written to the store — quarantine happens before persistence, not after.
**Read first:** `plan.md §5.4`, `plan.md §6` (`C-53`), `src/covenant_radar/security/uploads.py`, `crypto.py`.
**Contracts:** `C-53` `DocumentStore`, `C-04` `POST /documents`.
**Files owned:** `src/covenant_radar/ports/document_store.py`, `src/covenant_radar/documents/__init__.py`, `store.py`, `scan.py`, `src/covenant_radar/services/documents.py`, `src/covenant_radar/db/repositories/document.py`, `tests/unit/test_document_store.py`, `tests/integration/test_document_upload.py`
**Behaviour:** upload validated for type, size, extension and magic bytes; scanned; encrypted; written under a content-addressed key; a record created with its hash, type, size and retention class; retrieval streams without loading the whole file.
**Every case:** a scan failure → quarantined, never written to the store, and a security audit event raised; a re-upload of an identical file for the same borrower → recognised by content hash, the existing record returned, and no second copy stored; a file above the limit → refused with the limit stated and nothing written; the store unavailable → `StorageUnavailable` naming the path, and the upload refused rather than accepted-then-lost; a document type declared that disagrees with the magic bytes → refused naming both; retrieval of a missing key → `NotFound`, never an empty stream.
**Steps:** 1. Define the `DocumentStore` port. 2. Implement the filesystem backend with content-addressed keys and encryption. 3. Implement the validation and scan pipeline running before persistence. 4. Implement the service, repository and upload route. 5. Implement streaming retrieval.
**Tests:** `tests/unit/test_document_store.py` — `test_content_addressed_key`, `test_encrypted_at_rest`, `test_missing_key_not_found`, `test_streaming_does_not_load_whole_file`; `tests/integration/test_document_upload.py` — `test_scan_failure_quarantines_before_write`, `test_duplicate_returns_existing_no_second_copy`, `test_oversize_refused_nothing_written`, `test_type_mismatch_refused_naming_both`, `test_store_unavailable_refuses_upload`.
**Run:** `pytest -q tests/unit/test_document_store.py tests/integration/test_document_upload.py` 9 passed.
**Done when:** the nine tests pass and a failed scan leaves nothing in the store.
**Evidence:** the test output, a stored document's on-disk form showing encryption.

---

### [x] `T-085` · Native PDF text and span extraction
`Milestone: M-4` · `Builds: R-04` · `Days: 2.0` · `Depends on: T-084` · `Snapshot: var/snapshots/T-085/` · `Build: #18 · Phase 2 · cum 8.0h`

**Goal:** text with coordinates, so every field the product later extracts can point at the exact place on the page it came from.
**Context:** `spec §R-04.a` and `R-04.e`. Span provenance is what makes covenant intake auditable: without it, an approver is comparing a proposal against a document by hand.
**Read first:** `plan.md §5.4` (`document_page`, `document_span`), `src/covenant_radar/documents/store.py`.
**Contracts:** the spans `T-093` and `T-097` consume.
**Files owned:** `src/covenant_radar/documents/extract_native.py`, `spans.py`, `src/covenant_radar/services/documents.py` (extraction), `tests/unit/test_span_indexing.py`, `tests/integration/test_native_extraction.py`, `tests/fixtures/documents/*`
**Behaviour:** per page, text with character offsets and bounding boxes; spans addressable by page and offset range; reading order resolved for multi-column layouts; extraction state recorded on the document.
**Every case:** a two-column sanction letter → reading order resolved and asserted against a fixture, because a covenant clause read across columns is a wrong clause; a page with no extractable text → marked as needing OCR rather than recorded as empty; a rotated page → normalised with the rotation recorded; a span request beyond the page → refused naming the bounds; an encrypted or damaged PDF → refused naming the page, with no partial document record surviving.
**Steps:** 1. Implement per-page text and coordinate extraction. 2. Implement reading-order resolution for multi-column layouts. 3. Implement the span index and lookup. 4. Detect pages with no text and mark them for OCR. 5. Handle rotation, damage and encryption explicitly.
**Tests:** `tests/unit/test_span_indexing.py` — `test_span_lookup_by_offsets`, `test_out_of_bounds_refused`, `test_rotation_normalised_and_recorded`; `tests/integration/test_native_extraction.py` — `test_two_column_reading_order`, `test_no_text_page_marked_for_ocr`, `test_damaged_pdf_refused_naming_page`, `test_known_clause_span_resolves_to_expected_page_and_offsets`.
**Run:** `pytest -q tests/unit/test_span_indexing.py tests/integration/test_native_extraction.py` 7 passed.
**Done when:** the seven tests pass and a known clause in the fixture resolves to its expected span.
**Evidence:** the span resolution for the fixture clause.

---

### [x] `T-086` · OCR pipeline, page confidence, human-review routing
`Milestone: M-4` · `Builds: R-04` · `Days: 2.0` · `Depends on: T-085` · `Snapshot: var/snapshots/T-086/` · `Build: #19 · Phase 2 · cum 8.6h`

**Goal:** scanned sanction letters are readable, and pages the OCR is not confident about go to a person instead of quietly becoming wrong text.
**Context:** `spec §R-04.b` and T9's confidence floor. Tesseract availability is `plan.md [OPEN-13]`; take its default — native extraction only, scanned pages routed to review, and the limitation documented — if it cannot be installed.
**Read first:** `spec §17.5` (T9), `src/covenant_radar/documents/extract_native.py`, `config/settings.py` (capabilities).
**Contracts:** the pages `T-093` reads.
**Files owned:** `src/covenant_radar/documents/ocr.py`, `src/covenant_radar/services/documents.py` (OCR path), `src/covenant_radar/web/routes/documents.py` (review queue), `web/templates/screens/documents/_review.html`, `tests/integration/test_ocr.py`
**Behaviour:** OCR applied only to pages without extractable text; a per-page confidence stored; pages below T9 flagged `needs_review` and excluded from automated clause detection until a person confirms or corrects them; a review queue.
**Every case:** OCR unavailable → the capability reports so, scanned pages are flagged for review with the reason, and nothing pretends to have read them; a page exactly at the floor → used, because the floor is inclusive; a page below it → flagged and excluded from detection, and a test asserts the exclusion rather than only the flag; a mixed document → native pages used natively and only the scanned pages OCR'd; a reviewer correcting a page → the correction stored as a new version of the page text with provenance, the original retained.
**Steps:** 1. Detect which pages need OCR. 2. Run OCR with confidence capture. 3. Apply T9 and flag pages. 4. Exclude flagged pages from downstream detection. 5. Build the review queue and correction path with provenance. 6. Handle the capability-absent case.
**Tests:** `tests/integration/test_ocr.py` — `test_only_textless_pages_ocrd`, `test_confidence_stored_per_page`, `test_page_at_floor_used`, `test_below_floor_flagged_and_excluded_from_detection`, `test_capability_absent_flags_with_reason`, `test_correction_stored_as_new_version_original_retained`.
**Run:** `pytest -q tests/integration/test_ocr.py` 6 passed.
**Done when:** the six tests pass and a low-confidence page cannot reach clause detection.
**Evidence:** a mixed-document extraction report with per-page confidence.

---

### [x] `T-087` · Document classification and the span-highlighting viewer
`Milestone: M-4` · `Builds: R-04` · `Days: 1.5` · `Depends on: T-086, T-021` · `Snapshot: var/snapshots/T-087/` · `Build: #20 · Phase 2 · cum 9.1h`

**Goal:** a user clicks a field and sees the exact words on the page it came from.
**Context:** `spec §R-04.e`. This is the control that makes `T-097`'s approval step meaningful rather than ceremonial.
**Read first:** `src/covenant_radar/documents/spans.py`, `web/templates/_components/`.
**Contracts:** `C-02`'s document strip; the viewer route.
**Files owned:** `src/covenant_radar/documents/classify.py`, `src/covenant_radar/web/routes/documents.py` (viewer), `web/templates/screens/documents/*`, `web/static/js/document_viewer.js`, `tests/integration/test_document_viewer.py`
**Behaviour:** rule-based classification into sanction letter, amendment, compliance certificate, stock statement and other, with a confidence and a manual override; a viewer rendering the page with a highlighted span, addressable by URL so a link in a memo or a bundle opens the right place.
**Every case:** a classification below its confidence floor → recorded as unclassified and offered for manual selection, never guessed; a manual override → recorded with the actor and retained alongside the automatic result; a span link for a page later corrected by review → resolving to the corrected page with the correction noted; the viewer opened by a scoped user without access → `404`; script disabled → the page renders with the span's text quoted, so the provenance is still readable.
**Steps:** 1. Implement rule-based classification with a confidence and the unclassified state. 2. Implement manual override with audit. 3. Implement the viewer with server-rendered highlight and a URL-addressable span. 4. Implement the no-JavaScript quoted fallback. 5. Handle corrected pages.
**Tests:** `tests/integration/test_document_viewer.py` — `test_low_confidence_classification_unclassified_not_guessed`, `test_manual_override_recorded_alongside_automatic`, `test_span_url_opens_correct_page_and_highlight`, `test_corrected_page_noted`, `test_out_of_scope_404`, `test_no_javascript_quotes_span_text`.
**Run:** `pytest -q tests/integration/test_document_viewer.py` 6 passed.
**Done when:** the six tests pass and a span link is meaningful without script.
**Evidence:** a viewer screenshot with a highlighted covenant clause.

---

### [x] `T-088` · LLM provider protocol and the four adapters
`Milestone: M-4` · `Supports: plan §8.1` · `Days: 2.0` · `Depends on: T-004` · `Snapshot: var/snapshots/T-088/` · `Build: #12 · Phase 2 · cum 5.1h`

**Goal:** the provider is a configuration choice, which is the concrete form of the exit strategy the IT-Outsourcing Direction requires.
**Context:** `plan.md §8.1`. Four adapters: the TCS GenAI Lab gateway (default, OpenAI-compatible), Azure OpenAI, Anthropic, and `recorded` for cassette replay. An adapter never retries, never interprets, never repairs and never decides — those belong to the call site and the shape checker.
**Read first:** `plan.md §8.1`, `plan.md §6` (`C-50`), `src/covenant_radar/config/settings.py`.
**Contracts:** `C-50` `LLMProvider.complete`.
**Files owned:** `src/covenant_radar/ports/llm.py`, `src/covenant_radar/ai/__init__.py`, `ai/providers/base.py`, `tcs_genailab.py`, `azure_openai.py`, `anthropic.py`, `ai/errors.py`, `tests/unit/test_provider_protocol.py`, `tests/integration/test_providers.py`
**Behaviour:** one request and response shape across adapters, normalising the model identifier the provider actually returned, token counts, latency and the raw payload; provider selection from configuration; TLS verification always on.
**Every case:** a provider returning a non-conforming payload → returned as-is with a normalisation note, because refusing it is the shape checker's job and not the adapter's; an authentication failure → `ProviderAuthError`, never retried with altered credentials; a transport failure → `ProviderUnavailable` naming the provider but never the credential; a configuration selecting an unknown provider → refuse at startup listing the valid ones; any attempt to disable TLS verification → impossible in a released build, proven by a test.
**Steps:** 1. Define the protocol and the request and response shapes. 2. Implement the three live adapters over one HTTP client with per-provider request mapping. 3. Normalise responses including the returned model identifier. 4. Implement provider selection and startup validation. 5. Assert TLS verification cannot be disabled.
**Tests:** `tests/unit/test_provider_protocol.py` — `test_one_response_shape_across_adapters`, `test_adapter_does_not_retry`, `test_unknown_provider_refused_at_startup`, `test_tls_verification_cannot_be_disabled`; `tests/integration/test_providers.py` — `test_auth_failure_not_retried`, `test_transport_failure_names_provider_not_credential`, `test_non_conforming_payload_passed_through_with_note`.
**Run:** `pytest -q tests/unit/test_provider_protocol.py tests/integration/test_providers.py` 7 passed.
**Done when:** the seven tests pass and no adapter can disable TLS verification.
**Evidence:** the test output, the provider selection listing.

---

### [x] `T-089` · The single call site: retries, timeouts, ceilings, budget, logging
`Milestone: M-4` · `Supports: plan §8.2` · `Days: 1.5` · `Depends on: T-088` · `Snapshot: var/snapshots/T-089/` · `Build: #13 · Phase 2 · cum 5.6h`

**Goal:** one place in the whole codebase reaches a model, so every guarantee about model use is enforced once and provable by a scan.
**Context:** `plan.md §8.2` and import contract 3. The call site carries masking verification, prompt-version verification, the timeout, the single retry, the hourly, daily and budget ceilings, the cassette fallback, and the per-call record written on **every** path including refusals and ceiling hits.
**Read first:** `plan.md §8.2`, `plan.md §6` (`C-51`), `spec §17.5` (T7, T8), `src/covenant_radar/ai/providers/base.py`.
**Contracts:** `C-51` `ai.client.call`.
**Files owned:** `src/covenant_radar/ai/client.py`, `ai/budget.py`, `tests/unit/test_call_site.py`, `tests/integration/test_call_site_ceilings.py`, `tests/security/test_single_call_site.py`
**Behaviour:** `call(stage, prompt, prompt_version, context)` verifying the masking marker and the prompt version before sending, applying the timeout and the single retry, checking the ceilings and the budget before the call, falling back to a cassette where configured, and writing one `model_call` row on every path.
**Every case:** an unmasked prompt → `RuntimeError` before any network use, because an unmasked prompt must be impossible rather than merely discouraged; a stage outside the permitted set → `ValueError`; a prompt whose embedded version disagrees with its file → refused; the hourly, daily or monetary ceiling reached → `CeilingReached` with **no call made**, a record written and an alert raised; a timeout → one retry then `ProviderUnavailable`, with both attempts recorded; any module outside the two permitted callers importing this module → a failing test.
**Steps:** 1. Implement the pre-send verifications. 2. Implement the timeout and the single retry. 3. Implement the three ceilings and the budget check with queueing semantics. 4. Implement the cassette fallback. 5. Write the record on every path. 6. Write the scan test asserting the single call site.
**Tests:** `tests/unit/test_call_site.py` — `test_unmasked_prompt_raises_before_network`, `test_stage_outside_permitted_raises`, `test_version_mismatch_refused`, `test_timeout_retries_once_then_unavailable`, `test_every_path_writes_one_record`; `tests/integration/test_call_site_ceilings.py` — `test_hourly_ceiling_blocks_and_alerts`, `test_budget_ceiling_blocks_and_alerts`; `tests/security/test_single_call_site.py` — `test_only_permitted_modules_import_the_client`.
**Run:** `pytest -q tests/unit/test_call_site.py tests/integration/test_call_site_ceilings.py tests/security/test_single_call_site.py` 8 passed.
**Done when:** the eight tests pass and the scan proves exactly two modules import the client.
**Evidence:** the scan output, a model-call record from each path.

---

### [x] `T-090` · Outbound masking whitelist that fails closed
`Milestone: M-4` · `Supports: plan §8.3` · `Days: 1.5` · `Depends on: T-089` · `Snapshot: var/snapshots/T-090/` · `Build: #14 · Phase 2 · cum 6.1h`

**Goal:** the guarantee that no personal data leaves the host, built so that forgetting to whitelist a new field **fails** rather than leaks.
**Context:** `plan.md §8.3` and `spec §N-04.a`. The direction of the check is the whole design: an allow-list that raises on the unknown, never a deny-list that misses it.
**Read first:** `plan.md §8.3`, `plan.md §6` (`C-52`), `src/covenant_radar/security/crypto.py`, `spec §16.2`.
**Contracts:** `C-52` `ai.masking.build_outbound`.
**Files owned:** `src/covenant_radar/ai/masking.py`, `tests/unit/test_masking.py`, `tests/security/test_outbound_capture.py`
**Behaviour:** only whitelisted keys admitted, with anything else raising and naming the key; names replaced by role tokens; identifier patterns replaced by opaque tokens; the configured secret value scanned for and redacted; the token map kept on the host; a masking marker attached so the call site can verify it.
**Every case:** any key outside the whitelist → `FieldNotWhitelisted` naming it, and nothing sent; a nested structure → flattened and every leaf key checked, so nesting is not an escape; a name that is also an ordinary word → masked anyway, because a false positive costs a token and a false negative costs a contravention; a value containing the configured secret → redacted before return; a whitelisted field carrying an unexpected type → refused, since a free-text blob in a numeric field is how data leaks.
**Steps:** 1. Define the whitelist with a declared type per key. 2. Implement flattening and per-leaf checking. 3. Implement name and identifier masking with the local token map. 4. Implement secret redaction. 5. Attach the masking marker. 6. Write the outbound-capture test that runs a full workload and scans every captured body.
**Tests:** `tests/unit/test_masking.py` — `test_unknown_key_raises_and_sends_nothing`, `test_nested_leaves_checked`, `test_names_and_identifiers_masked`, `test_secret_value_redacted`, `test_wrong_type_refused`, `test_token_map_stays_local`; `tests/security/test_outbound_capture.py` — `test_full_workload_capture_has_zero_personal_fields`, `test_full_workload_capture_has_zero_secret_material`.
**Run:** `pytest -q tests/unit/test_masking.py tests/security/test_outbound_capture.py` 8 passed.
**Done when:** the eight tests pass and the full-workload capture is clean.
**Evidence:** the capture scan report.

---

### [x] `T-091` · Recorded-response adapter and cassette management
`Milestone: M-4` · `Supports: plan §8.1` · `Days: 0.5` · `Depends on: T-088` · `Snapshot: var/snapshots/T-091/` · `Build: #15 · Phase 2 · cum 6.3h`

**Goal:** the whole evaluation suite and CI run with no network, using real recorded responses — the same mechanism that lets the product run air-gapped.
**Context:** `plan.md §8.1`. Cassettes are a first-class adapter, not a test fixture, which is why they live in the package and are keyed by the masked prompt so a replayed answer is provably the answer to the same question.
**Read first:** `src/covenant_radar/ai/providers/base.py`, `ai/masking.py`.
**Contracts:** `C-50` implemented by the recorded adapter.
**Files owned:** `src/covenant_radar/ai/providers/recorded.py`, `src/covenant_radar/cli.py` (the cassette group), `evaluation/cassettes/.gitkeep`, `tests/unit/test_cassettes.py`
**Behaviour:** responses stored keyed by the hash of the masked prompt and the prompt version; a record mode capturing live responses; a replay mode; a miss returning a clear absence rather than a fabricated response.
**Every case:** a cassette miss → an explicit miss the caller handles as provider-unavailable, never an empty or invented response; a prompt version bump → a miss, because the key includes the version and an answer to a different question is not an answer; a corrupt cassette file → skipped with a warning, the rest usable; record mode with no provider configured → refused; a cassette containing personal data → impossible, since it stores the already-masked prompt, and a test asserts it.
**Steps:** 1. Implement the key from the masked prompt and version. 2. Implement record and replay modes. 3. Implement the explicit miss. 4. Implement the cassette CLI group. 5. Assert cassettes carry only masked content.
**Tests:** `tests/unit/test_cassettes.py` — `test_round_trip`, `test_miss_is_explicit_not_fabricated`, `test_version_bump_is_a_miss`, `test_corrupt_file_skipped`, `test_cassette_contains_only_masked_content`.
**Run:** `pytest -q tests/unit/test_cassettes.py` 5 passed.
**Done when:** the five tests pass and a cassette can never contain unmasked content.
**Evidence:** a cassette file, the miss behaviour transcript.

---

### [x] `T-092` · Prompt files, version binding and the build check
`Milestone: M-4` · `Supports: plan §8.4` · `Days: 1.0` · `Depends on: T-089` · `Snapshot: var/snapshots/T-092/` · `Build: #16 · Phase 2 · cum 6.6h`

**Goal:** a prompt is a versioned artefact, and a version that lies is caught by the build rather than discovered in an audit.
**Context:** `plan.md §8.4` and `spec §N-12.b`. The first line of each prompt file carries its version; the client refuses a mismatch; and a content-hash check fails the build when a prompt changes without a version bump.
**Read first:** `plan.md §8.4`, `src/covenant_radar/ai/client.py`.
**Contracts:** the prompt-version verification in `C-51`.
**Files owned:** `src/covenant_radar/ai/prompts/__init__.py`, `loader.py`, `stage1_extract.v1.md`, `stage7_memo.v1.md`, `prompt_hashes.json`, `tests/unit/test_prompts.py`
**Behaviour:** prompts loaded by name and version, their embedded version checked against the filename, their content hash checked against the recorded manifest; the manifest updated only by an explicit command that requires a version bump.
**Every case:** a prompt edited without a version bump → the hash check fails naming the file and the expected version; a version bump with no content change → refused, since a version that means nothing is worse than none; a prompt file with no version header → refused at load; a prompt requested at a version that does not exist → refused listing the available versions; a template placeholder in a prompt that no caller supplies → refused at load, because a prompt with an unfilled slot produces a subtly wrong answer.
**Steps:** 1. Write the two prompt files with version headers, fixed output shapes and the refusal instruction for redirection attempts. 2. Implement the loader with version and hash verification. 3. Implement placeholder validation against the declared slot set. 4. Implement the manifest-update command requiring a bump. 5. Add the check to the gate.
**Tests:** `tests/unit/test_prompts.py` — `test_edit_without_bump_fails`, `test_bump_without_change_refused`, `test_missing_version_header_refused`, `test_unknown_version_lists_available`, `test_unfilled_placeholder_refused`, `test_both_prompts_declare_output_shape`.
**Run:** `pytest -q tests/unit/test_prompts.py` 6 passed · `python -m radarctl gate --fast` includes the prompt check.
**Done when:** the six tests pass and a prompt cannot change without a version bump.
**Evidence:** the hash manifest, a deliberate-edit failure transcript.

---

### [x] `T-093` · Clause candidate detection over documents and text
`Milestone: M-4` · `Builds: R-06` · `Days: 1.5` · `Depends on: T-085` · `Snapshot: var/snapshots/T-093/` · `Build: #21 · Phase 2 · cum 9.6h`

**Goal:** find the parts of a sanction letter that might be covenants, so the model is asked about clauses rather than about a forty-page document.
**Context:** narrowing before extraction is what keeps the model's task bounded, the prompt small and the cost low, and it is deterministic code rather than a model call.
**Read first:** `src/covenant_radar/documents/spans.py`, `domain/ratios/library.py`.
**Contracts:** the candidates `T-094` consumes.
**Files owned:** `src/covenant_radar/domain/intake/__init__.py`, `candidates.py`, `src/covenant_radar/services/intake.py` (detection), `tests/unit/test_clause_detection.py`, `tests/integration/test_detection_recall.py`
**Behaviour:** rule-based detection over section headings, financial-covenant vocabulary, ratio names, comparison language and threshold patterns in Indian sanction-letter idiom; each candidate carrying its span and the rules that matched.
**Every case:** a page flagged as needing review by OCR → excluded from detection entirely, and a test asserts it; a candidate spanning a page break → captured whole with both page references; a document with no candidates → reported as none found with the rules tried, so a user knows the difference between "no covenants" and "detection failed"; a clause matching several rules → one candidate carrying all matches, never duplicated; recall measured against the fixture set and reported.
**Steps:** 1. Build the vocabulary and pattern rules for Indian sanction-letter idiom. 2. Implement detection with span capture and page-break handling. 3. Record the matching rules per candidate. 4. Exclude review-flagged pages. 5. Measure recall against the labelled fixture documents.
**Tests:** `tests/unit/test_clause_detection.py` — `test_review_flagged_pages_excluded`, `test_page_break_candidate_captured_whole`, `test_multiple_rules_one_candidate`, `test_no_candidates_reports_rules_tried`; `tests/integration/test_detection_recall.py` — `test_recall_on_fixture_documents_meets_floor`.
**Run:** `pytest -q tests/unit/test_clause_detection.py tests/integration/test_detection_recall.py` 5 passed.
**Done when:** the five tests pass and recall on the fixture set meets its recorded floor.
**Evidence:** the recall report.

---

### [x] `T-094` · Stage-1 proposal, parsing and normalisation
`Milestone: M-4` · `Builds: R-06` · `Days: 1.5` · `Depends on: T-093, T-092` · `Snapshot: var/snapshots/T-094/` · `Build: #22 · Phase 2 · cum 10.1h`

**Goal:** the model's contribution — a structured proposal per candidate clause — parsed strictly and normalised into the shape the verifications will test.
**Context:** `spec §17.1`. The model proposes; it decides nothing. The proposal is a hypothesis, and `T-095` is the disproof.
**Read first:** `src/covenant_radar/ai/client.py`, `ai/prompts/stage1_extract.v1.md`, `domain/intake/candidates.py`.
**Contracts:** `C-51`; `C-52`; the proposal shape `T-095` consumes.
**Files owned:** `src/covenant_radar/ai/intake.py`, `src/covenant_radar/domain/intake/proposal.py`, `tests/unit/test_stage1_parsing.py`, `tests/integration/test_stage1_proposal.py`
**Behaviour:** one masked call per candidate; the reply parsed against the declared output shape; fields normalised — thresholds to `Decimal` with a unit, frequencies and directions to their enumerations, dates to the FY calendar; the raw reply retained for the trace.
**Every case:** a reply that is not valid structured output → the proposal is marked unparseable with the parse error as its detail, never partially trusted; a threshold expressed in words or with a currency symbol → normalised where unambiguous and flagged where not, never guessed; a frequency the model expressed ambiguously → carried through as ambiguous for `T-095` to fail, not resolved here; a reply proposing a definition outside the library → carried through for verification to refuse, because refusal must be a code decision; the provider unavailable → propagated so the caller can render hand entry.
**Steps:** 1. Build the masked prompt per candidate. 2. Call through the single call site. 3. Parse strictly against the declared shape. 4. Normalise unambiguous values and flag ambiguity. 5. Retain the raw reply for the trace.
**Tests:** `tests/unit/test_stage1_parsing.py` — `test_unparseable_reply_marked_not_partially_trusted`, `test_threshold_normalisation_unambiguous`, `test_ambiguous_value_flagged_not_guessed`, `test_out_of_library_definition_carried_for_verification`; `tests/integration/test_stage1_proposal.py` — `test_one_call_per_candidate`, `test_raw_reply_retained`, `test_provider_unavailable_propagates`.
**Run:** `pytest -q tests/unit/test_stage1_parsing.py tests/integration/test_stage1_proposal.py` 7 passed.
**Done when:** the seven tests pass and no refusal decision is taken in this module.
**Evidence:** a proposal set from the fixture sanction letter.

---

### [x] `T-095` · The six code verifications, failing closed
`Milestone: M-4` · `Builds: R-06` · `Days: 2.0` · `Depends on: T-094, T-034` · `Snapshot: var/snapshots/T-095/` · `Build: #23 · Phase 2 · cum 10.8h`

**Goal:** the product's distinctive move — the code independently disproving what the model proposed, so only what the arithmetic can reproduce ever becomes a record.
**Context:** `spec §R-06` names six verifications: schema validity, definition in the library or a valid custom formula, definition **actually recomputable against this borrower's stored statements**, threshold within the definition's plausible band, units and currency consistent, and frequency and effective dates allowed and consistent with the facility. The third is the one that matters most and the one no competitor was found to do.
**Read first:** `spec §R-06`, `src/covenant_radar/domain/ratios/compute.py`, `domain/intake/proposal.py`, `db/seed/data/ratio_definitions.json`.
**Contracts:** `C-06`'s failure shape.
**Files owned:** `src/covenant_radar/domain/intake/verify.py`, `src/covenant_radar/ai/shapes.py` (stage-1 checks), `tests/unit/test_verification.py`, `tests/integration/test_verification_closed.py`
**Behaviour:** all six run, all six results collected rather than stopping at the first, and `all_passed` set only when every one passed; each result naming its check, its verdict and a human-readable detail.
**Every case:** an implausible threshold → the range check fails naming the band and the observed value; a definition the library knows but that cannot be computed against this borrower's statements because a line is missing → the recomputability check fails naming the missing line, which is the check that catches a plausible-looking but inapplicable proposal; a unit mismatch, such as a ratio threshold expressed in ₹ crore → refused; an effective date before the facility's sanction → refused naming both; an ambiguous frequency → refused, not resolved; text attempting to redirect the model → refused with the fixed refusal and a security audit event; every failure leaving `all_passed` false and no covenant created anywhere.
**Steps:** 1. Implement each of the six as an independent function returning a named result. 2. Run all six and collect. 3. Implement the recomputability check by actually invoking the ratio computation against the borrower's stored periods. 4. Implement the injection refusal path with its audit event. 5. Assert that no path creates a covenant when any check failed.
**Tests:** `tests/unit/test_verification.py` — `test_all_six_run_and_collect`, `test_implausible_threshold_named_with_band`, `test_missing_line_fails_recomputability_naming_it`, `test_unit_mismatch_refused`, `test_effective_date_before_sanction_refused`, `test_ambiguous_frequency_refused_not_resolved`; `tests/integration/test_verification_closed.py` — `test_injection_refused_and_audited`, `test_no_covenant_created_on_any_failure`.
**Run:** `pytest -q tests/unit/test_verification.py tests/integration/test_verification_closed.py` 8 passed.
**Done when:** the eight tests pass and no failing proposal can produce a covenant by any path.
**Evidence:** a verification report for a deliberately wrong clause.

---

### [x] `T-096` · Intake service, confirm refusal and the approval flow
`Milestone: M-4` · `Builds: R-06` · `Days: 1.5` · `Depends on: T-095, T-033` · `Snapshot: var/snapshots/T-096/` · `Build: #24 · Phase 2 · cum 11.3h`

**Goal:** the confirmation path, where a human confirms only what code could reproduce, and where the refusal is structural rather than a rendered state.
**Context:** `spec §16.1`: confirming a covenant that failed verification is marked as permitted to no role in any configuration. The control not rendering is necessary but not sufficient; the endpoint must refuse too, and a test must prove it.
**Read first:** `src/covenant_radar/domain/intake/verify.py`, `services/registry.py`, `security/maker_checker.py`.
**Contracts:** `C-05`, `C-06`, `C-07`; `C-60`.
**Files owned:** `src/covenant_radar/services/intake.py`, `src/covenant_radar/db/repositories/proposal.py`, `tests/integration/test_intake_service.py`, `tests/security/test_confirm_refusal.py`
**Behaviour:** proposals persisted with their verification results; submission of a passing proposal creating a covenant version, routed through maker-checker where enabled; a corrected field triggering re-verification before submission; every step audited.
**Every case:** a direct request to confirm a failed proposal → `409` naming the failed checks, regardless of role, session or API key, proven per role; a field corrected by the officer → all six checks re-run, never trusting the prior verdict; a proposal for a covenant that already exists on the facility → offered as an amendment rather than a duplicate; a proposal abandoned → retained with its verification results, because a rejected proposal is evidence about the document; the same document submitted twice → the prior proposals shown rather than re-extracted, unless re-extraction is explicitly requested.
**Steps:** 1. Persist proposals with results and spans. 2. Implement correction with mandatory re-verification. 3. Implement submission with the failed-proposal refusal and maker-checker routing. 4. Implement duplicate and amendment detection. 5. Audit every step.
**Tests:** `tests/integration/test_intake_service.py` — `test_correction_reverifies_all_six`, `test_existing_covenant_offered_as_amendment`, `test_abandoned_proposal_retained`, `test_resubmitted_document_shows_prior_proposals`, `test_every_step_audited`; `tests/security/test_confirm_refusal.py` — `test_confirm_failed_proposal_refused_for_every_role`, `test_refusal_names_failed_checks`.
**Run:** `pytest -q tests/integration/test_intake_service.py tests/security/test_confirm_refusal.py` 7 passed.
**Done when:** the seven tests pass and no role can confirm a failed proposal through any surface.
**Evidence:** the per-role refusal matrix output.

---

### [x] `T-097` · Intake screen: side-by-side, inline verdicts, hand entry
`Milestone: M-4` · `Builds: R-24` · `Days: 2.0` · `Depends on: T-096, T-087` · `Snapshot: var/snapshots/T-097/` · `Build: #51 · Phase 4 · cum 24.2h`

**Goal:** the screen where a credit officer checks a proposal against its source text — clause left, fields right, verdicts inline, confirm only when green.
**Context:** `spec §15.4`: intake is organised around *checking a proposal against its source text*. `spec §R-24.b`: a failed proposal renders struck with the failing check named and **no confirm control anywhere in the document**.
**Read first:** `src/covenant_radar/services/intake.py`, `web/templates/screens/documents/`, `spec §15.4`.
**Contracts:** `C-04`, `C-05`, `C-06`, `C-07`.
**Files owned:** `src/covenant_radar/web/routes/intake.py`, `web/templates/screens/intake/*`, `web/view_models/intake.py`, `tests/integration/test_intake_screen.py`, `tests/e2e/test_intake_flow.py`
**Behaviour:** upload with progress and OCR status; per candidate, the source span on the left and the proposed fields on the right; a verdict mark per check with its detail; the confirm control rendered inside a conditional on all-passed; hand entry on the same screen with the same verifications; bulk confirm for a multi-clause document.
**Every case:** a failed proposal → struck, the failing check named, and no confirm control in the document at all, asserted by parsing the markup rather than by checking a class; the provider unavailable → the hand-entry form on the same screen with the proposal column absent and the verifications still running; a page needing OCR review → the candidate marked as pending review rather than proposed; bulk confirm with one failure among many → the passing ones confirmable and the failing one clearly excluded; a clause span clicked → the document viewer opening at the highlight.
**Steps:** 1. Write the upload and status view. 2. Write the two-column layout with the span on the left. 3. Render verdict marks with their details. 4. Render the confirm control only inside the all-passed conditional. 5. Wire hand entry and bulk confirm. 6. Wire the span link to the viewer.
**Tests:** `tests/integration/test_intake_screen.py` — `test_clean_clause_renders_all_green_and_confirm`, `test_failed_proposal_struck_with_check_named`, `test_no_confirm_control_in_markup_when_failed`, `test_provider_down_renders_hand_entry_with_verification`, `test_review_pending_candidate_not_proposed`, `test_bulk_confirm_excludes_failures`; `tests/e2e/test_intake_flow.py` — `test_upload_to_live_covenant_flow`, `test_span_click_opens_viewer`.
**Run:** `pytest -q tests/integration/test_intake_screen.py tests/e2e/test_intake_flow.py` 8 passed · screenshots at three viewports in both themes.
**Done when:** the eight tests pass and the confirm control is absent from the markup, not merely hidden.
**Evidence:** the screenshots including a struck proposal.

---

### [x] `T-098` · Action catalogue: model, management and applicability
`Milestone: M-4` · `Builds: R-16` · `Days: 1.5` · `Depends on: T-062` · `Snapshot: var/snapshots/T-098/` · `Build: #55 · Phase 5 · cum 26.2h`

**Goal:** the bounded, role-tagged, configurable set of things the product may recommend — so a recommendation is chosen from a list the bank owns rather than composed by a model.
**Context:** `spec §R-16.c`: an action `T-063` cannot simulate cannot be recommended, so the effect model is required. `spec §R-16.b`: a retired entry still resolves for historical memos.
**Read first:** `plan.md §5.7` (`intervention`), `src/covenant_radar/domain/interventions/effects.py`, `spec §16.1`.
**Contracts:** the catalogue `C-08`'s memo and `C-11`'s simulation read.
**Files owned:** `src/covenant_radar/domain/interventions/catalogue.py`, `src/covenant_radar/services/catalogue.py`, `src/covenant_radar/db/seed/data/interventions.json`, `src/covenant_radar/web/routes/catalogue.py`, `web/templates/screens/admin/_catalogue.html`, `tests/integration/test_catalogue.py`
**Behaviour:** entries with id, role tag, text, effect model and parameters, applicable covenant classes, approval requirement and active state; a shipped default set covering relationship-manager, credit and risk actions; management under maker-checker; retirement rather than deletion.
**Every case:** an entry saved with no effect model → refused naming the rule; an entry retired → still resolvable by historical memos and simulations, and excluded from new recommendations; an entry whose applicable classes are empty → refused, since an action applicable to nothing is dead configuration; a role tag outside the three → refused; a change → routed through maker-checker and audited.
**Steps:** 1. Define the entry model and validation. 2. Seed the default catalogue across the three roles. 3. Implement management with maker-checker. 4. Implement retirement with historical resolution. 5. Implement applicability filtering for recommendation and simulation.
**Tests:** `tests/integration/test_catalogue.py` — `test_entry_without_effect_model_refused`, `test_retired_entry_resolves_historically_and_is_excluded_from_new`, `test_empty_applicability_refused`, `test_invalid_role_tag_refused`, `test_change_routed_through_maker_checker_and_audited`, `test_default_set_covers_three_roles`.
**Run:** `pytest -q tests/integration/test_catalogue.py` 6 passed.
**Done when:** the six tests pass and no unsimulatable action can be recommended.
**Evidence:** the seeded catalogue listing.

---

### [x] `T-099` · Memo slot assembly from records only
`Milestone: M-4` · `Builds: R-17` · `Days: 1.5` · `Depends on: T-058, T-064` · `Snapshot: var/snapshots/T-099/` · `Build: #56 · Phase 5 · cum 26.7h`

**Goal:** every figure the memo will contain, assembled from records with a reference back to each one, **before** any model is involved.
**Context:** `spec §17.1`: any figure the product presents as true comes from a record or a calculation, never from the model. That guarantee is created here, by giving the model figures it cannot change and a template it cannot restructure.
**Read first:** `plan.md §5.8` (`memo`), `src/covenant_radar/services/scoring.py`, `services/simulation.py`.
**Contracts:** the slot map `C-08` renders and `T-100` checks against.
**Files owned:** `src/covenant_radar/domain/memo/__init__.py`, `slots.py`, `template.py`, `src/covenant_radar/services/memo.py` (assembly), `tests/unit/test_memo_slots.py`
**Behaviour:** a fixed template with named sections — situation, covenant position, drivers, evidence citations, simulated options with assumptions, recommended interventions, advisory closing — and a slot map from every named slot to its value and the record reference that produced it.
**Every case:** a slot whose record is absent → the slot carries the documented absence text and its reason, never an empty string that reads as zero; a suppressed forecast → the probability slot carries the suppression text and its limiting factor; a borrower with no simulations → the options section carries the documented absence rather than being omitted, so the template shape is stable; every slot resolving to a reference, asserted by a test; no slot value computed here that is not already in a record, asserted by a test.
**Steps:** 1. Define the template sections and the slot names. 2. Assemble each slot from records with its reference. 3. Handle absence and suppression explicitly. 4. Assert that assembly computes nothing. 5. Provide the catalogue's applicable actions as slot data.
**Tests:** `tests/unit/test_memo_slots.py` — `test_every_slot_carries_a_record_reference`, `test_absent_record_uses_documented_absence_text`, `test_suppressed_forecast_slot_carries_reason`, `test_no_simulations_section_still_present`, `test_assembly_computes_no_value`, `test_template_sections_fixed`.
**Run:** `pytest -q tests/unit/test_memo_slots.py` 6 passed.
**Done when:** the six tests pass and no slot value originates outside a record.
**Evidence:** a slot map for the hero borrower with every reference resolved.

---

### [x] `T-100` · Stage-7 prompt, drafting and the four shape checks
`Milestone: M-4` · `Builds: R-17` · `Days: 2.0` · `Depends on: T-099, T-092` · `Snapshot: var/snapshots/T-100/` · `Build: #57 · Phase 5 · cum 27.4h`

**Goal:** the model writes the connecting prose and nothing else, and the check that proves it did — a numeric token in the prose that is not in a slot refuses the memo.
**Context:** `plan.md §8.5`. Four checks: every slot resolved, every action in the catalogue with the right role tag, length within T6, and **no numeric token outside a slot**. The last is the one that stops an invented figure reaching a screen.
**Read first:** `plan.md §8.5`, `src/covenant_radar/domain/memo/slots.py`, `ai/client.py`, `ai/prompts/stage7_memo.v1.md`.
**Contracts:** `C-08`; `C-51`; `C-52`.
**Files owned:** `src/covenant_radar/ai/memo.py`, `src/covenant_radar/ai/shapes.py` (stage-7 checks), `ai/prompts/stage7_memo.v1.md`, `tests/unit/test_memo_shapes.py`, `tests/integration/test_memo_drafting.py`
**Behaviour:** the masked prompt carrying the slot values and the permitted action ids; the reply checked by all four before anything is stored or shown; the model's text marked so the interface can label it.
**Every case:** a numeric token in the prose not present in any slot → the check fails naming the token, and this is tested with a deliberately fabricated figure; an action cited that is not in the catalogue → fails; an action cited with the wrong role tag → fails; length above T6 → one shorter regeneration then refusal; a reply that reformats a slot value, such as changing a date's form → treated as a mismatch and failed, because a figure the model rewrote is a figure the model touched; directive language in a memo required to be advisory → fails.
**Steps:** 1. Write the stage-7 prompt with the fixed sections, the given figures, the permitted action ids and the advisory-tone instruction. 2. Build the masked prompt from the slot map. 3. Implement the four checks, each returning a named result. 4. Implement numeric-token extraction and slot matching with exact-form comparison. 5. Implement the directive-language check.
**Tests:** `tests/unit/test_memo_shapes.py` — `test_fabricated_figure_fails`, `test_reformatted_slot_value_fails`, `test_action_outside_catalogue_fails`, `test_wrong_role_tag_fails`, `test_length_above_t6_fails`, `test_directive_language_fails`; `tests/integration/test_memo_drafting.py` — `test_clean_draft_passes_all_four`, `test_masked_prompt_carries_only_whitelisted_fields`.
**Run:** `pytest -q tests/unit/test_memo_shapes.py tests/integration/test_memo_drafting.py` 8 passed.
**Done when:** the eight tests pass and a fabricated figure cannot survive the check.
**Evidence:** the check output for a deliberately fabricated draft.

---

### [x] `T-101` · Memo refusal, retry and persistence rules
`Milestone: M-4` · `Builds: R-17` · `Days: 1.0` · `Depends on: T-100` · `Snapshot: var/snapshots/T-101/` · `Build: #58 · Phase 5 · cum 27.7h`

**Goal:** a failing draft is retried once and then refused, and a refused memo leaves **no record at all**, so a half-finished memo simply does not exist.
**Context:** `spec §R-17.b`. The absence of a partial memo is a stronger guarantee than a warning banner on one, and it is enforced by writing the record only after the checks pass.
**Read first:** `src/covenant_radar/ai/memo.py`, `spec §17.5` (T8), `plan.md §5.8`.
**Contracts:** `C-08`'s failure behaviour; `C-60`.
**Files owned:** `src/covenant_radar/services/memo.py` (persistence and refusal), `src/covenant_radar/db/repositories/memo.py`, `tests/integration/test_memo_refusal.py`
**Behaviour:** on failure, one regeneration with the failure detail fed back as a constraint; on second failure, a refusal with the reason, no memo record written, and a stage-7 trace row recording the refusal; on success, the memo persisted with its slot map, drafted text, actions, verdict and versions.
**Every case:** two failures → the user sees the refusal, `memo` has no new row, and a test counts rows before and after; the provider unavailable → the degraded message and everything else on the screen still working; the ceiling reached → queued with a banner, and the queued request resolvable later; a memo generated then the underlying forecast superseded → the memo retained referencing the run it used, because that is what it said at the time; the refusal itself audited and traced, so "why is there no memo" is answerable.
**Steps:** 1. Implement the single regeneration with the failure fed back. 2. Implement refusal with no persistence. 3. Persist only on success with every version stamped. 4. Write the stage-7 trace row on both paths. 5. Handle the unavailable and ceiling paths.
**Tests:** `tests/integration/test_memo_refusal.py` — `test_two_failures_write_no_memo_row`, `test_refusal_traced_and_audited`, `test_regeneration_feeds_back_failure_detail`, `test_provider_unavailable_degrades_screen_intact`, `test_ceiling_queues_with_banner`, `test_memo_retained_after_forecast_superseded`.
**Run:** `pytest -q tests/integration/test_memo_refusal.py` 6 passed.
**Done when:** the six tests pass and a refused memo leaves no row.
**Evidence:** the before-and-after row counts, a refusal trace.

---

### [x] `T-102` · Memo PDF and DOCX export with integrity hash
`Milestone: M-4` · `Builds: R-17` · `Days: 1.5` · `Depends on: T-101` · `Snapshot: var/snapshots/T-102/` · `Build: #64 · Phase 6 · cum 31.0h`

**Goal:** the artefact that leaves the product and enters a committee pack, carrying its own provenance.
**Context:** `spec §R-17.d`: the export contains the same figures, the integrity hash, the timestamp and the generating user.
**Read first:** `src/covenant_radar/services/memo.py`, `plan.md §5.8` (`memo_export`), `web/static/css/print.css`.
**Contracts:** `C-09` memo export.
**Files owned:** `src/covenant_radar/documents/render.py`, `src/covenant_radar/services/memo.py` (export), `src/covenant_radar/web/templates/exports/memo.html`, `tests/integration/test_memo_export.py`
**Behaviour:** PDF and DOCX rendered from the stored memo with configurable letterhead, page numbering, the generation timestamp in IST, the generating user, the integrity hash and a footer marking the model-drafted sections; each export recorded.
**Every case:** the same memo exported twice → identical content, and the hash proving it, with only the export timestamp differing and recorded separately; a memo whose simulations are cited → their assumptions printed, never summarised away; letterhead unconfigured → a plain professional default, never a broken layout; an export by a user without the permission → refused; the exported figures compared to the stored slot map by a test, so a rendering bug cannot silently alter a number.
**Steps:** 1. Write the export template reusing the print styles. 2. Implement PDF rendering with pagination and the footer. 3. Implement DOCX rendering with the same content. 4. Compute and embed the integrity hash. 5. Record each export. 6. Write the figure-comparison test.
**Tests:** `tests/integration/test_memo_export.py` — `test_pdf_and_docx_contain_same_figures_as_slots`, `test_integrity_hash_stable_across_exports`, `test_assumptions_printed_in_full`, `test_default_letterhead_when_unconfigured`, `test_permission_enforced`, `test_export_recorded`.
**Run:** `pytest -q tests/integration/test_memo_export.py` 6 passed.
**Done when:** the six tests pass and the exported figures match the stored slots exactly.
**Evidence:** a rendered memo PDF with its hash.

---

### [x] `T-103` · Evaluation example schema and the authored set
`Milestone: M-4` · `Builds: N-01` · `Days: 2.0` · `Depends on: T-041` · `Snapshot: var/snapshots/T-103/` · `Build: #61 · Phase 6 · cum 29.4h`

**Goal:** the versioned, hand-labelled cases the product is scored on, in a shape where adding one is a file rather than a code change.
**Context:** `spec §17.7`. Coverage across extraction, engine exactness, boundary behaviour, persistence and materiality, forecast dating, false escalation, grounding, refusal and usefulness.
**Read first:** `spec §17.7`, `evaluation/reference_portfolio/labels.py`, `src/covenant_radar/domain/ratios/`.
**Contracts:** `C-79`'s example discovery.
**Files owned:** `evaluation/examples/_schema.json`, `evaluation/examples/EX-*.json`, `tests/unit/test_examples.py`
**Behaviour:** one file per example carrying its id, kind, which arms it applies to, its input, its hand-labelled expectation and its pass mark; a schema every file matches; ids unique and stable.
**Every case:** a hand-labelled expectation disagreeing with the implementation → **the example is right until proved otherwise**; the disagreement is recorded in the file's note and raised for a human decision, never silently edited to match the code; a malformed file → named and skipped by the runner with the run continuing; a duplicate id → a build failure; an example whose input references reference-portfolio data → resolved by label rather than by hard-coded value, so a regeneration does not invalidate it; adversarial extraction cases included, with redirection attempts and implausible thresholds.
**Steps:** 1. Write the schema. 2. Author the engine and boundary cases with hand-worked arithmetic in each file's note. 3. Author the extraction cases in Indian sanction-letter idiom, including the adversarial ones. 4. Author the forecast dating, false-escalation and grounding cases against the labelled cohorts. 5. Author the refusal and usefulness cases with the rubric criteria. 6. Validate every file against the schema.
**Tests:** `tests/unit/test_examples.py` — `test_every_file_matches_schema`, `test_ids_unique`, `test_engine_examples_recompute_exactly`, `test_examples_reference_labels_not_hardcoded_values`, `test_coverage_across_every_category`, `test_adversarial_extraction_cases_present`.
**Run:** `pytest -q tests/unit/test_examples.py` 6 passed.
**Done when:** every file validates, every category is covered, and every expectation is hand-labelled rather than copied from a run.
**Evidence:** the coverage summary by category.

---

### [x] `T-104` · Evaluation harness: the product arm
`Milestone: M-4` · `Builds: N-01` · `Days: 2.0` · `Depends on: T-103, T-091` · `Snapshot: var/snapshots/T-104/` · `Build: DONE`

**Goal:** one command scores the real pipeline against every example, offline, deterministically.
**Context:** `spec §N-01.c`: the suite runs with no network. That is what cassettes are for, and it is why the harness is usable in CI on every commit rather than occasionally by hand.
**Read first:** `evaluation/examples/_schema.json`, `src/covenant_radar/services/`, `ai/providers/recorded.py`.
**Contracts:** `C-79` `python -m evaluation.run`.
**Files owned:** `evaluation/__init__.py`, `run.py`, `score.py`, `arms/__init__.py`, `arms/product.py`, `tests/integration/test_harness_product.py`
**Behaviour:** discovery of examples, dispatch by kind to the real pipeline, scoring against each pass mark, and a printed table plus a stored run record with the commit reference.
**Every case:** a malformed example → named and skipped, the run continuing, and the skip counted in the summary; a stage not yet built → those examples skipped with the reason, the rest scored, so the harness is useful from the first day it exists; a cassette miss → that example skipped with the reason rather than reaching the network, and the offline guard proving no call was attempted; a miss against a pass mark → printed, never hidden, and the run still exiting zero without `--gate`; the run itself breaking → a distinct non-zero exit, so a bad score and a broken harness are never confused.
**Steps:** 1. Implement discovery and validation. 2. Implement dispatch by kind through the real services. 3. Implement scoring per pass mark. 4. Implement the table and the stored run record. 5. Implement the exit-code contract. 6. Assert offline operation.
**Tests:** `tests/integration/test_harness_product.py` — `test_all_examples_scored`, `test_malformed_example_skipped_and_counted`, `test_unbuilt_stage_skips_only_its_examples`, `test_cassette_miss_skips_without_network`, `test_miss_printed_and_exits_zero`, `test_broken_run_exits_distinctly`, `test_run_record_carries_commit`.
**Run:** `python -m evaluation.run` prints the table and exits 0 · `pytest -q tests/integration/test_harness_product.py` 7 passed.
**Done when:** the seven tests pass and the harness runs with no network access.
**Evidence:** a scored run table.

---

### [x] `T-105` · Evaluation harness: the baseline arm and the scoreboard
`Milestone: M-4` · `Builds: N-01` · `Days: 1.5` · `Depends on: T-104` · `Snapshot: var/snapshots/T-105/` · `Build: DONE`

**Goal:** the honest alternative, scored on the same examples, so the product's number has something beside it.
**Context:** `spec §3.3` and `§17.7`. The baseline is the naive headroom rule for forecasting, a regex parser for extraction, and one ungrounded prompt for the memo. This is the falsification test the specification commits to: if the mechanism is no better, the extra stages are decoration.
**Read first:** `evaluation/arms/product.py`, `spec §17.7`.
**Contracts:** `C-79`'s `--both-arms`.
**Files owned:** `evaluation/arms/baseline.py`, `evaluation/report.py`, `tests/integration/test_harness_baseline.py`
**Behaviour:** the three baselines implemented honestly — a genuine attempt, not a straw man — scored identically, with both arms printed side by side against the pass marks and the gap reported per category.
**Every case:** the baseline outscoring the product on any category → reported prominently rather than buried, because that is the finding the harness exists to surface; a baseline that cannot attempt a category → recorded as not applicable, not as a zero, since a zero would flatter the product; the same examples and the same scoring applied to both arms, asserted by a test; the report stored per run with the commit, so the trend over releases is visible.
**Steps:** 1. Implement the naive headroom rule. 2. Implement the regex extraction parser as a genuine attempt. 3. Implement the ungrounded memo prompt. 4. Score both arms identically. 5. Write the side-by-side report with per-category gaps. 6. Assert scoring symmetry.
**Tests:** `tests/integration/test_harness_baseline.py` — `test_both_arms_use_identical_scoring`, `test_baseline_win_reported_prominently`, `test_not_applicable_is_not_zero`, `test_report_stored_with_commit`, `test_per_category_gap_computed`.
**Run:** `python -m evaluation.run --both-arms` prints both arms and exits 0 · `pytest -q tests/integration/test_harness_baseline.py` 5 passed.
**Done when:** the five tests pass and the baseline is a genuine attempt rather than a straw man.
**Evidence:** the two-arm scoreboard.

---

### [x] `T-106` · Regression gates and score floors in CI
`Milestone: M-4` · `Builds: N-01` · `Days: 0.5` · `Depends on: T-105` · `Snapshot: var/snapshots/T-106/` · `Build: DONE`

**Goal:** a score that drops fails the build, so quality is a gate rather than a report someone reads later.
**Context:** `spec §N-01.d`. Floors move only upward, and only with a recorded justification, because a floor that can be lowered to pass is not a floor.
**Read first:** `evaluation/run.py`, `.github/workflows/ci.yml`.
**Contracts:** `C-79`'s `--gate`.
**Files owned:** `evaluation/floors.json`, `evaluation/run.py` (gate mode), `.github/workflows/ci.yml` (the evaluation job), `tests/unit/test_score_floors.py`
**Behaviour:** recorded floors per category; `--gate` exiting non-zero when any score is below its floor, naming the category, the floor and the observed score; a floor-raising command requiring a justification string that is stored.
**Every case:** a score below its floor → the build fails naming all three numbers; a score above → the floor is not raised automatically, because a lucky run should not become a commitment; a floor lowered by editing the file → refused by a test comparing against the recorded history; a new category with no floor → the build fails asking for one, since an unfloored category is an ungated one.
**Steps:** 1. Record the current floors from the calibrated run. 2. Implement gate mode with clear failure output. 3. Implement the floor-raising command with a stored justification. 4. Add the monotonicity test over the floor history. 5. Add the job to CI.
**Tests:** `tests/unit/test_score_floors.py` — `test_below_floor_fails_naming_numbers`, `test_floors_never_lowered`, `test_new_category_without_floor_fails`, `test_raise_requires_justification`.
**Run:** `python -m evaluation.run --both-arms --gate` exit 0 · `pytest -q tests/unit/test_score_floors.py` 4 passed.
**Done when:** the four tests pass and the gate is in CI.
**Evidence:** the floors file, a deliberate-regression failure transcript.

---

### [x] `T-107` · Model registry, model cards and the approval path
`Milestone: M-4` · `Builds: N-12` · `Days: 1.5` · `Depends on: T-089` · `Snapshot: var/snapshots/T-107/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** every model-using component is on a register with an owner and an approval, because an unapproved model in production use is the finding an inspector is looking for.
**Context:** `spec §N-12.a`. This satisfies the FREE-AI expectation of a model inventory with named accountability and proportionate oversight.
**Read first:** `plan.md §5.9` (`model_registration`), `src/covenant_radar/ai/client.py`, `security/maker_checker.py`.
**Contracts:** the registry `T-081`'s governance screen reads.
**Files owned:** `src/covenant_radar/ai/registry.py`, `src/covenant_radar/services/model_governance.py`, `docs/model-cards/*`, `tests/integration/test_model_registry.py`
**Behaviour:** one registration per model-using component carrying provider, model identifier, prompt version, purpose, owner, evaluation run and approval; the call site refusing to use an unregistered or unapproved component in production; a model card per component.
**Every case:** a call from an unregistered component → refused in production with the component named, and permitted with a loud warning in development, so the constraint is real without blocking work; a registration approved by its own owner → refused where maker-checker requires a distinct approver; a prompt version bump → the registration marked as requiring re-approval, because the approved thing has changed; a component with no model card → the build fails; the register readable by an auditor with no write path.
**Steps:** 1. Implement the registration model and the registry. 2. Enforce registration and approval at the call site. 3. Route approval through maker-checker. 4. Mark re-approval needed on version change. 5. Write the model cards and the build check for their presence.
**Tests:** `tests/integration/test_model_registry.py` — `test_unregistered_component_refused_in_production`, `test_self_approval_refused`, `test_prompt_bump_requires_reapproval`, `test_missing_model_card_fails_build`, `test_auditor_read_only`, `test_registration_audited`.
**Run:** `pytest -q tests/integration/test_model_registry.py` 6 passed.
**Done when:** the six tests pass and an unapproved component cannot be used in production.
**Evidence:** the register listing, the model cards.

---

### T-108 · Drift monitoring, guardrails and automatic rollback
`Milestone: M-4` · `Builds: N-12` · `Days: 1.5` · `Depends on: T-107, T-105` · `Snapshot: var/snapshots/T-108/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the product notices when its own behaviour changes, and returns to a known-good state without waiting for a person.
**Context:** `spec §N-12.c`. Drift metrics are input distribution, override rate, refusal rate and evaluation score — the four that would move first if something silently went wrong.
**Read first:** `plan.md §5.9` (`drift_observation`), `src/covenant_radar/ai/registry.py`, `evaluation/run.py`.
**Contracts:** the observations `T-081`'s screen and `T-145`'s alerts read.
**Files owned:** `src/covenant_radar/ai/drift.py`, `src/covenant_radar/services/model_governance.py` (drift and rollback), `tests/integration/test_drift.py`
**Behaviour:** scheduled observation of the four metrics against a recorded baseline window; a breach raising an alert and, where the guardrail is configured to act, pinning the prior approved version and recording the rollback; every observation retained.
**Every case:** a metric breaching its guardrail → alert raised, rollback executed where configured, both audited, and the governance screen showing the state; a rollback with no prior approved version → alert only, with the reason, never a rollback to nothing; a breach caused by a genuine change in the portfolio rather than the model → still alerted, because the system cannot tell the difference and a human must look; drift computed on too small a window → reported as insufficient data rather than a spurious breach; the baseline window itself updated only by an approved action.
**Steps:** 1. Implement the four metric computations over a configurable window. 2. Implement baseline comparison and breach detection with a minimum sample size. 3. Implement alerting. 4. Implement guarded automatic rollback with audit. 5. Handle the no-prior-version case. 6. Gate baseline updates behind approval.
**Tests:** `tests/integration/test_drift.py` — `test_breach_alerts_and_rolls_back_where_configured`, `test_no_prior_version_alerts_only`, `test_insufficient_window_reports_not_breaches`, `test_baseline_update_requires_approval`, `test_every_observation_retained`, `test_rollback_audited`.
**Run:** `pytest -q tests/integration/test_drift.py` 6 passed.
**Done when:** the six tests pass and a simulated breach rolls back correctly. **Superseded: see the Phase 5 gate in §4.**
**Evidence:** a simulated drift breach with its rollback record, the M-4 gate record.

---

### M-5 · Workflow, integration and platform — 51.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] T-109 · Case model, SLA derivation and lifecycle
`Milestone: M-5` · `Builds: R-18` · `Days: 2.0` · `Depends on: T-059` · `Snapshot: var/snapshots/T-109/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a warning somebody owns, with a clock on it — the record that answers "what did the bank do about it".
**Context:** `spec §R-18`. SLA hours by band come from T11. Case history is append-only, because a case whose history can be edited is not evidence.
**Read first:** `plan.md §5.8` (`case`, `case_event`), `spec §17.5` (T11), `src/covenant_radar/services/triage.py`.
**Contracts:** `C-14`; `C-60`.
**Files owned:** `src/covenant_radar/domain/cases/__init__.py`, `lifecycle.py`, `sla.py`, `src/covenant_radar/services/cases.py`, `src/covenant_radar/db/repositories/case.py`, `tests/unit/test_case_lifecycle.py`, `tests/integration/test_case_service.py`
**Behaviour:** a case opened or updated when a borrower enters or moves within the act or amber bands, with the assignee from the portfolio mapping and the due date from T11; states open, in progress, monitoring, escalated and closed with documented permitted transitions; append-only history.
**Every case:** a borrower re-entering the act band while a case is open → the existing case updated, never a second case, because two cases for one borrower is two people doing the same work; a closed case whose borrower re-escalates → a new case linked to the prior, both retained; an SLA passing its due date → the case escalated, listed as overdue and included in the escalation digest; an undocumented state transition → refused naming the permitted ones; a case closed with no reason → refused; the assignee absent from the portfolio mapping → assigned to the portfolio's default owner and an administrator notified, never left unassigned.
**Steps:** 1. Define the state machine with permitted transitions. 2. Implement SLA derivation from T11 with the business-calendar convention. 3. Implement open-or-update on band entry. 4. Implement escalation on SLA breach. 5. Implement append-only history. 6. Handle the missing-assignee case.
**Tests:** `tests/unit/test_case_lifecycle.py` — `test_permitted_transitions_only`, `test_sla_from_band`, `test_closure_requires_reason`; `tests/integration/test_case_service.py` — `test_reentry_updates_existing_case`, `test_reescalation_after_closure_links_prior`, `test_sla_breach_escalates_and_lists_overdue`, `test_missing_assignee_falls_to_default_and_notifies`, `test_history_append_only`.
**Run:** `pytest -q tests/unit/test_case_lifecycle.py tests/integration/test_case_service.py` 8 passed.
**Done when:** the eight tests pass and no borrower can have two open cases.
**Evidence:** a case lifecycle trace.

---

### [x] T-110 · Case screens, comments, actions taken
`Milestone: M-5` · `Builds: R-18` · `Days: 1.5` · `Depends on: T-109, T-075` · `Snapshot: var/snapshots/T-110/` · `Build: DONE`

**Goal:** the working surface where a case is actually handled, and where the intervention actually taken is recorded — the raw material of the value measurement.
**Context:** `spec §G2` measures recovery value against interventions taken, which only works if taking one is recorded here rather than in somebody's inbox.
**Read first:** `src/covenant_radar/services/cases.py`, `web/templates/screens/borrower/`.
**Contracts:** `C-14`.
**Files owned:** `src/covenant_radar/web/routes/cases.py`, `web/templates/screens/cases/*`, `web/view_models/case.py`, `tests/integration/test_case_screens.py`
**Behaviour:** a case list scoped to the caller with filters; a case detail with history, comments, linked memos, simulations and documents; assignment, state change, comment and log-action controls rendered by permission.
**Every case:** a comment mentioning a user outside the case's scope → the mention is stored but no notification is sent, and the author is told, because a notification is a disclosure; an action logged citing a catalogue intervention → linked, with free text also permitted and marked as such; a state change the role may not make → the control absent and the endpoint refusing; a case with a superseded memo → the memo shown with its run marked superseded; the history rendered in one order with no editing control anywhere.
**Steps:** 1. Write the list and detail routes and view models. 2. Write the screens using only components. 3. Implement comment with mention resolution and the scope rule. 4. Implement log-action with catalogue linkage. 5. Render controls by permission.
**Tests:** `tests/integration/test_case_screens.py` — `test_list_scoped_and_filtered`, `test_out_of_scope_mention_not_notified_and_author_told`, `test_logged_action_links_to_catalogue`, `test_state_control_absent_without_permission`, `test_superseded_memo_marked`, `test_no_history_editing_control`.
**Run:** `pytest -q tests/integration/test_case_screens.py` 6 passed.
**Done when:** the six tests pass and case history has no editing path.
**Evidence:** the screenshots.

---

### [x] `T-111` · Override capture and view revision
`Milestone: M-5` · `Builds: R-19` · `Days: 1.5` · `Depends on: T-067, T-071` · `Snapshot: var/snapshots/T-111/` · `Build: #68 · Phase 6 · cum 32.6h`

**Goal:** a risk officer can disagree, in a way that changes the view and keeps both states — the property `problem.md` asks for and most systems lack.
**Context:** `spec §R-19` and `spec §16.1`: only the risk roles may override, never silently, and a reason is mandatory.
**Read first:** `plan.md §5.8` (`override_record`), `src/covenant_radar/services/explain.py`, `security/permissions.py`.
**Contracts:** `C-12` `POST /overrides`; `C-60`.
**Files owned:** `src/covenant_radar/services/overrides.py`, `src/covenant_radar/db/repositories/override.py`, `src/covenant_radar/web/routes/overrides.py`, `web/templates/_components/override_form.html`, `tests/integration/test_overrides.py`
**Behaviour:** an override recording what was shown, what the user did instead, the stage, the reason, the prompt and model versions where applicable and the threshold snapshot; the displayed view revised; the original retained and reconstructable.
**Every case:** no reason → refused and nothing written; a stage outside the range → refused; the same subject overridden twice → both retained, the later shown, and the reconstruction showing the sequence; an override on a subject the caller cannot see → `404`; a reason containing a name or an identifier → stored locally, never sent to any provider, asserted by a test against the masking whitelist; the override visible on the case file and in the why-panel, not hidden in an audit screen.
**Steps:** 1. Implement recording with the full context capture. 2. Implement view revision that reads the latest override without mutating the underlying record. 3. Enforce the mandatory reason and the permission. 4. Surface overrides on the case file and the why-panel. 5. Assert the reason never reaches an outbound path.
**Tests:** `tests/integration/test_overrides.py` — `test_missing_reason_refused_nothing_written`, `test_both_states_reconstructable`, `test_second_override_shown_sequence_retained`, `test_out_of_scope_404`, `test_reason_never_in_outbound_whitelist`, `test_override_visible_on_case_file_and_why_panel`.
**Run:** `pytest -q tests/integration/test_overrides.py` 6 passed.
**Done when:** the six tests pass and an override never mutates the underlying record.
**Evidence:** a before-and-after reconstruction across an override.

---

### [x] T-112 · Disposition, feedback and labelled-dataset export
`Milestone: M-5` · `Builds: R-19` · `Days: 1.5` · `Depends on: T-111` · `Snapshot: var/snapshots/T-112/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** what the desk did with each warning, captured lightly enough that people actually do it, and exportable as the labelled dataset a second version learns from.
**Context:** `spec §17.7`'s loop back. `spec §6` G3 counts the acted-on share, which only exists if dispositions are recorded.
**Read first:** `plan.md §5.8` (`disposition`), `src/covenant_radar/services/overrides.py`.
**Contracts:** `C-13` `POST /dispositions`.
**Files owned:** `src/covenant_radar/services/dispositions.py`, `src/covenant_radar/web/routes/dispositions.py`, `web/templates/_components/feedback_control.html`, `src/covenant_radar/services/labelled_export.py`, `tests/integration/test_dispositions.py`
**Behaviour:** a lightweight control on the case file and under the memo recording acted, monitoring or dismissed with a reason code; an export producing one row per warning with its features, its disposition and, where available, its outcome.
**Every case:** a dismissal with no reason code → refused, because an uncoded dismissal teaches nothing; a disposition changed later → both retained with the sequence; an export containing a personal-class value → impossible, since the export carries references and derived features only, asserted by a test; a warning with no disposition → present in the export as unlabelled rather than omitted, because absence of a label is itself informative; the export scoped, so a user exports only what they may see.
**Steps:** 1. Implement recording with the reason-code taxonomy. 2. Build the control and place it on both surfaces. 3. Implement the export with feature assembly. 4. Exclude personal-class values from the export. 5. Include unlabelled warnings explicitly.
**Tests:** `tests/integration/test_dispositions.py` — `test_dismissal_requires_reason_code`, `test_change_retains_sequence`, `test_export_has_no_personal_value`, `test_unlabelled_warnings_present`, `test_export_scoped`, `test_control_on_both_surfaces`.
**Run:** `pytest -q tests/integration/test_dispositions.py` 6 passed.
**Done when:** the six tests pass and the export carries no personal-class value.
**Evidence:** an export sample.

---

### [x] T-113 · Admin console: users, roles, scoping, sessions
`Milestone: M-5` · `Builds: R-26` · `Days: 2.0` · `Depends on: T-016, T-022` · `Snapshot: var/snapshots/T-113/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** an administrator runs the system from the product rather than from the database.
**Context:** `spec §R-26.d`: an administrator cannot grant themselves a role they do not hold without a second approver. Privilege escalation by the person who manages privileges is the classic weakness, and it is closed here.
**Read first:** `src/covenant_radar/security/rbac.py`, `maker_checker.py`, `db/scoping.py`.
**Contracts:** `C-17`; `C-60`.
**Files owned:** `src/covenant_radar/services/admin_users.py`, `src/covenant_radar/web/routes/admin.py` (users), `web/templates/screens/admin/users/*`, `tests/integration/test_admin_users.py`, `tests/security/test_privilege_escalation.py`
**Behaviour:** user creation, role assignment, portfolio scoping, deactivation, password reset, session listing and revocation, and SSO mapping configuration.
**Every case:** an administrator granting themselves a role they do not hold → routed through maker-checker and refused without a distinct approver; a user deactivated → sessions revoked immediately and API keys disabled; a role removed → the user's sessions revoked so the change takes effect at once; a scope narrowed → saved views referencing lost portfolios handled per `T-074`; the last active administrator being deactivated → refused, because a system with no administrator cannot be recovered from inside; every change audited with before and after.
**Steps:** 1. Implement the service with the escalation guard. 2. Implement session and key revocation on change. 3. Implement the last-administrator guard. 4. Write the screens. 5. Audit every change with before and after.
**Tests:** `tests/integration/test_admin_users.py` — `test_deactivation_revokes_sessions_and_keys`, `test_role_change_revokes_sessions`, `test_last_administrator_protected`, `test_every_change_audited_with_before_and_after`; `tests/security/test_privilege_escalation.py` — `test_self_grant_requires_distinct_approver`, `test_no_route_bypasses_the_guard`.
**Run:** `pytest -q tests/integration/test_admin_users.py tests/security/test_privilege_escalation.py` 8 passed.
**Done when:** the six tests pass and self-escalation is impossible without a second approver.
**Evidence:** the escalation-refusal transcript.

---

### [x] T-114 · Admin console: thresholds with approval, action catalogue
`Milestone: M-5` · `Builds: R-26` · `Days: 1.5` · `Depends on: T-113, T-098` · `Snapshot: var/snapshots/T-114/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the numbers and the recommendations the bank owns are edited by the bank, under approval, with history.
**Context:** `spec §R-26.a`: a threshold change enters pending and takes effect only on approval, with both recorded. The mechanism exists from `T-012`; this is the surface.
**Read first:** `src/covenant_radar/config/thresholds.py`, `services/catalogue.py`, `web/templates/screens/governance/`.
**Contracts:** `C-18`.
**Files owned:** `src/covenant_radar/web/routes/admin.py` (configuration), `web/templates/screens/admin/config/*`, `web/view_models/admin_config.py`, `tests/integration/test_admin_config.py`
**Behaviour:** a threshold editor showing current value, boundary behaviour and effect, with a preview of how many borrowers would change band; a proposal and approval flow; catalogue management with the effect-model requirement.
**Every case:** a proposed change → its band-change preview computed against the latest run and shown before submission, because a threshold change with an unknown blast radius is a guess; a proposal violating an invariant → refused naming it before submission; a proposal approved → applied at the next run and the applying run recorded, never retroactively rewriting past forecasts; a catalogue entry saved without an effect model → refused; every change audited with the actor, the approver and the reason.
**Steps:** 1. Write the threshold editor with boundary explanations. 2. Implement the band-change preview. 3. Wire proposal and approval. 4. Write catalogue management. 5. Make the application point explicit and recorded.
**Tests:** `tests/integration/test_admin_config.py` — `test_preview_shows_band_change_count`, `test_invariant_violation_refused_before_submission`, `test_approval_applies_at_next_run_not_retroactively`, `test_catalogue_entry_without_effect_model_refused`, `test_changes_audited_with_actor_and_approver`.
**Run:** `pytest -q tests/integration/test_admin_config.py` 5 passed.
**Done when:** the five tests pass and no approved change rewrites a past forecast.
**Evidence:** a preview and an approval trail.

---

### [x] T-115 · Admin console: jobs, health, retention configuration
`Milestone: M-5` · `Builds: R-26` · `Days: 1.0` · `Depends on: T-113` · `Snapshot: var/snapshots/T-115/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the operational surface — what ran, what failed, what is queued, what is due to be purged, and what the system's own health is.
**Context:** `spec §R-26.c`: a failed job is visible with its error and is retryable, without anyone opening a log file.
**Read first:** `plan.md §5.9` (`job_run`, `retention_purge_log`), `src/covenant_radar/observability/health.py`.
**Contracts:** `C-20`; `C-23`.
**Files owned:** `src/covenant_radar/web/routes/admin.py` (operations), `web/templates/screens/admin/ops/*`, `web/view_models/admin_ops.py`, `tests/integration/test_admin_ops.py`
**Behaviour:** job history with duration, outcome and error; manual trigger and retry; a health panel showing every capability and dependency; quarantine and entity-match queue depths; retention policy configuration with the next purge preview.
**Every case:** a job currently running → the manual trigger refused with the running instance named, never a second concurrent run; a failed job retried → a new run linked to the failed one, both retained; a capability unconfigured → shown as such with what configuring it would enable, not as an error; a retention change → previewed by count per entity before it applies; a purge already executed → shown in the log and never re-runnable.
**Steps:** 1. Write the job history and controls with the concurrency guard. 2. Write the health panel from the capabilities and dependency checks. 3. Surface queue depths. 4. Write retention configuration with the purge preview. 5. Show the purge log.
**Tests:** `tests/integration/test_admin_ops.py` — `test_manual_trigger_refused_while_running`, `test_retry_links_to_failed_run`, `test_unconfigured_capability_shown_not_errored`, `test_retention_change_previews_counts`, `test_purge_log_not_rerunnable`.
**Run:** `pytest -q tests/integration/test_admin_ops.py` 5 passed.
**Done when:** the five tests pass and no job can run twice concurrently.
**Evidence:** the screenshots.

---

### [x] T-116 · Notification model, templates, preferences, quiet hours
`Milestone: M-5` · `Builds: R-27` · `Days: 1.5` · `Depends on: T-010` · `Snapshot: var/snapshots/T-116/` · `Build: DEFERRED — out of scope for this window`

**Goal:** one notification pipeline every channel shares, with the recipient's preferences and the disclosure rules applied once rather than per channel.
**Context:** `spec §R-27.d`: no personal-class field appears in any notification body beyond what the recipient's role may already see. A notification is a disclosure, and it must respect scope.
**Read first:** `plan.md §5.8` (`notification`, `notification_preference`), `plan.md §6` (`C-54`), `src/covenant_radar/security/rbac.py`.
**Contracts:** `C-54` `Notifier`.
**Files owned:** `src/covenant_radar/ports/notifier.py`, `src/covenant_radar/notifications/__init__.py`, `model.py`, `templates/*`, `preferences.py`, `src/covenant_radar/services/notifications.py`, `tests/unit/test_notification_model.py`, `tests/integration/test_notification_scope.py`
**Behaviour:** typed templates with declared data slots; recipient resolution with a scope filter applied to the content; per-user, per-template, per-channel preferences with quiet hours and digest frequency; queueing with a scheduled send time.
**Every case:** content a recipient's scope excludes → removed from the body, and if nothing remains the notification is not sent and the suppression is recorded, because sending an empty alert is worse than none; quiet hours → deferred to the window's end rather than dropped; a template with an unfilled slot → refused at render, never sent partially; a recipient deactivated between queueing and sending → not sent, and the suppression recorded; a preference disabling a template → respected, except for the security and system-failure templates, which are documented as non-suppressible.
**Steps:** 1. Define the port and the typed templates with declared slots. 2. Implement recipient resolution and scope-based content filtering. 3. Implement preferences, quiet hours and the non-suppressible set. 4. Implement queueing with scheduled send. 5. Refuse unfilled slots at render.
**Tests:** `tests/unit/test_notification_model.py` — `test_unfilled_slot_refused`, `test_quiet_hours_defer_not_drop`, `test_non_suppressible_templates_documented`; `tests/integration/test_notification_scope.py` — `test_out_of_scope_content_removed`, `test_empty_after_filtering_not_sent_and_recorded`, `test_deactivated_recipient_not_sent`.
**Run:** `pytest -q tests/unit/test_notification_model.py tests/integration/test_notification_scope.py` 6 passed.
**Done when:** the six tests pass and no notification discloses beyond the recipient's scope.
**Evidence:** a scope-filtered notification body.

---

### [x] T-117 · Email digests and bundling
`Milestone: M-5` · `Builds: R-27` · `Days: 1.5` · `Depends on: T-116` · `Snapshot: var/snapshots/T-117/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the morning queue arrives in the inbox as one email, not a hundred — which is the difference between a useful alert and an ignored one.
**Context:** `spec §R-27.a`: a band change produces exactly one bundled digest entry per subscribed recipient. `spec §2` row 8's alert fatigue is the failure this design is defending against.
**Read first:** `src/covenant_radar/notifications/model.py`, `services/triage.py`, `config/settings.py` (SMTP capability).
**Contracts:** `C-54`.
**Files owned:** `src/covenant_radar/notifications/email.py`, `digest.py`, `templates/email/*`, `tests/integration/test_email_digest.py`
**Behaviour:** digests for the morning queue, band changes, SLA breaches, certificate due and overdue, and job failures; bundling by recipient and window; plain-text and HTML parts; deep links back into the product.
**Every case:** a hundred changes in one window → one email with a hundred entries, proven by a test; a recipient with nothing to report → no email, because an empty digest trains people to ignore digests; SMTP unconfigured → notifications queue and surface in-app, with the administrator told what is unconfigured; a send failure → retried per `T-118`'s policy and never silently lost; an HTML part that fails to render → the plain-text part still sent, since a degraded email beats none.
**Steps:** 1. Implement the digest assembler with windowing and bundling. 2. Write the templates with both parts. 3. Implement the SMTP sender with the capability check. 4. Implement the empty-digest suppression. 5. Implement deep links carrying the subject reference.
**Tests:** `tests/integration/test_email_digest.py` — `test_hundred_changes_one_email`, `test_empty_digest_not_sent`, `test_smtp_unconfigured_queues_and_tells_admin`, `test_html_failure_still_sends_plain_text`, `test_deep_links_resolve`.
**Run:** `pytest -q tests/integration/test_email_digest.py` 5 passed.
**Done when:** the five tests pass and a hundred changes produce one email.
**Evidence:** a rendered digest, both parts.

---

### [x] T-118 · Webhook delivery: signing, retry, dead letter
`Milestone: M-5` · `Builds: R-27` · `Days: 1.5` · `Depends on: T-116` · `Snapshot: var/snapshots/T-118/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the bank's own systems can receive events, verifiably, without anything being lost when an endpoint is down.
**Context:** `spec §R-27.b`: three failures land in the dead-letter queue with an alert, and nothing is lost.
**Read first:** `src/covenant_radar/notifications/model.py`, `security/crypto.py`.
**Contracts:** `C-54`.
**Files owned:** `src/covenant_radar/notifications/webhook.py`, `src/covenant_radar/services/notifications.py` (webhook path), `docs/api/webhooks.md`, `tests/integration/test_webhooks.py`
**Behaviour:** signed payloads with a timestamp and a replay window; retry with exponential backoff to the configured count; a dead-letter queue with an alert and manual replay; delivery status visible to an administrator.
**Every case:** three failures → dead-lettered, alerted, and replayable by hand without duplicating an already-delivered event, which the delivery record prevents; a slow endpoint → timed out and retried, never blocking the pipeline; a signature verified by a receiver using the documented procedure, tested against a reference implementation in the documentation; a payload containing a personal-class field → impossible, since the payload carries references and derived values, asserted by a test; an endpoint removed while events are queued → the queue drained to the dead letter with the reason.
**Steps:** 1. Implement signing with a timestamp and the documented verification procedure. 2. Implement asynchronous delivery with backoff. 3. Implement the dead-letter queue with alerting and manual replay. 4. Implement delivery records and the duplicate guard. 5. Write the receiver documentation with a reference verification snippet.
**Tests:** `tests/integration/test_webhooks.py` — `test_three_failures_dead_letter_and_alert`, `test_manual_replay_does_not_duplicate`, `test_signature_verifies_with_documented_procedure`, `test_payload_has_no_personal_field`, `test_removed_endpoint_drains_with_reason`, `test_slow_endpoint_does_not_block_pipeline`.
**Run:** `pytest -q tests/integration/test_webhooks.py` 6 passed.
**Done when:** the six tests pass and nothing is lost on repeated failure.
**Evidence:** a dead-letter record, a verified signature.

---

### [x] `T-119` · In-app notification centre
`Milestone: M-5` · `Builds: R-27` · `Days: 1.0` · `Depends on: T-116, T-022` · `Snapshot: var/snapshots/T-119/` · `Build: DONE — pulled forward ahead of schedule; see MERGE_LOG.md`

**Goal:** the channel that always works, including when SMTP is not configured and the customer has not finished their integration.
**Context:** `spec §12.1`'s [OPEN-07] means email may not be available at first. The in-app centre is the default so the product is useful on day one.
**Read first:** `src/covenant_radar/notifications/model.py`, `web/templates/base.html`.
**Contracts:** the notification resources.
**Files owned:** `src/covenant_radar/notifications/inapp.py`, `src/covenant_radar/web/routes/notifications.py`, `web/templates/screens/notifications/*`, `tests/integration/test_inapp_notifications.py`
**Behaviour:** an unread count in the shell, a list with filters, mark-as-read individually and in bulk, and deep links; every notification retained per the retention schedule.
**Every case:** a notification whose subject the user has since lost access to → shown without its content and marked as no longer accessible, never leaking the content; marking all as read → recorded as one action with a count, not one event per notification; the count computed within the latency budget on an account with thousands of notifications, proven by a test; script disabled → the centre works as a normal page.
**Steps:** 1. Implement storage, listing and the unread count with an index. 2. Implement mark-as-read individually and in bulk. 3. Handle lost access. 4. Add the count to the shell. 5. Ensure the no-JavaScript path.
**Tests:** `tests/integration/test_inapp_notifications.py` — `test_lost_access_hides_content_not_existence`, `test_bulk_read_is_one_action`, `test_count_within_budget_at_scale`, `test_works_without_javascript`, `test_deep_links_resolve`.
**Run:** `pytest -q tests/integration/test_inapp_notifications.py` 5 passed.
**Done when:** the five tests pass and the count is fast at scale.
**Evidence:** the timing, the screenshots.

---

### [x] `T-120` · Job model, scheduler, run ledger, restart resumption
`Milestone: M-5` · `Builds: R-28` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-120/` · `Build: #59 · Phase 6 · cum 28.2h`

**Goal:** scheduled work that survives a restart, records what it did, and never runs twice concurrently.
**Context:** `spec §R-28`. A database-backed job store is what makes a restart resume rather than repeat, and the run ledger is what makes last night's batch answerable this morning.
**Read first:** `plan.md §5.9` (`job_run`), `src/covenant_radar/db/session.py`, `config/settings.py`.
**Contracts:** `C-20`, `C-74` `radarctl job run`.
**Files owned:** `src/covenant_radar/scheduler/__init__.py`, `jobs.py`, `runner.py`, `ledger.py`, `src/covenant_radar/cli.py` (the job group), `tests/integration/test_scheduler.py`
**Behaviour:** a job registry with schedules from configuration; a database-backed store; a run ledger recording start, finish, outcome, attempt, error and metrics; a concurrency lock per job; graceful shutdown finishing or cleanly abandoning in-flight work.
**Every case:** a restart mid-run → the run marked interrupted and resumed or restarted per the job's declared policy, never left ambiguous; two schedulers started against one database → the lock ensures one runner, and the second reports why it is idle; a job raising → the run recorded as failed with the error and the retry policy applied; a job with no declared policy → refused at registration, since an undeclared failure policy is a decision made at 3 a.m. by nobody; a manual trigger while scheduled → refused with the running instance named.
**Steps:** 1. Define the job registry with declared schedule, timeout, retry and interruption policy. 2. Implement the database-backed store and the per-job lock. 3. Implement the ledger. 4. Implement graceful shutdown. 5. Implement the CLI trigger with the concurrency guard.
**Tests:** `tests/integration/test_scheduler.py` — `test_restart_resumes_or_restarts_per_policy`, `test_second_runner_idles_with_reason`, `test_failure_recorded_with_error_and_retried`, `test_job_without_policy_refused_at_registration`, `test_manual_trigger_refused_while_running`, `test_graceful_shutdown_finishes_or_abandons_cleanly`.
**Run:** `pytest -q tests/integration/test_scheduler.py` 6 passed.
**Done when:** the six tests pass and a hard restart never produces a duplicate run.
**Evidence:** a run ledger across a simulated restart.

---

### [x] `T-121` · Nightly pipeline composition and idempotent re-run
`Milestone: M-5` · `Builds: R-28` · `Days: 2.0` · `Depends on: T-120, T-060` · `Snapshot: var/snapshots/T-121/` · `Build: #60 · Phase 6 · cum 28.7h`

**Goal:** the queue is ready before anyone logs in — ingest, test, score, forecast, attribute, rank, update cases, dispatch — as ordered, individually retryable steps.
**Context:** `spec §R-28.a` and `R-28.c`: a full run inside its window, and the same run triggered twice producing identical results and no duplicate notifications.
**Read first:** `src/covenant_radar/scheduler/jobs.py`, every service the pipeline calls.
**Contracts:** `C-20`.
**Files owned:** `src/covenant_radar/scheduler/pipeline.py`, `src/covenant_radar/services/nightly.py`, `tests/integration/test_nightly_pipeline.py`
**Behaviour:** the ordered steps as separate jobs sharing one pipeline run id; each step idempotent by run id; a manual trigger for one borrower or the whole book; the run marked complete only when every step has succeeded.
**Every case:** the same pipeline run triggered twice → identical outputs by content hash and **no second set of notifications**, which is the failure users notice fastest; a step failing → the run halted at that step with the state recorded, the prior day's results still serving the queue, and nothing half-scored presented as today's; a single-borrower run → touching only that borrower and not creating a portfolio-wide run record; a run spanning a threshold change → the snapshot captured at the start and used throughout, so a run is internally consistent; a step retried after partial completion → resuming without redoing committed work.
**Steps:** 1. Compose the steps with a shared run id. 2. Make each step idempotent by run id. 3. Implement the halt-on-failure policy preserving the prior day. 4. Capture the threshold snapshot once at the start. 5. Implement single-borrower and full-book triggers. 6. Guard notification dispatch against re-run.
**Tests:** `tests/integration/test_nightly_pipeline.py` — `test_rerun_identical_and_no_duplicate_notifications`, `test_step_failure_halts_and_preserves_prior_day`, `test_single_borrower_run_scoped`, `test_snapshot_captured_once_at_start`, `test_retry_resumes_without_redoing_committed_work`, `test_completion_requires_every_step`.
**Run:** `pytest -q tests/integration/test_nightly_pipeline.py` 6 passed · a full run over the reference portfolio inside its window.
**Done when:** the six tests pass and a re-run sends no second notification.
**Evidence:** two run ledgers with identical content hashes.

---

### [x] T-122 · Partial-failure policy, retry and deadline alerting
`Milestone: M-5` · `Builds: R-28` · `Days: 1.5` · `Depends on: T-121` · `Snapshot: var/snapshots/T-122/` · `Build: implemented out-of-order this session, pulled forward from §2.4's deferred list at explicit request; verified with pytest/ruff/mypy directly, not run through the numbered §2.3 build order or python -m radarctl gate (git and the AI provider key are unavailable in this environment)`

**Goal:** the batch never leaves the portfolio half-scored, and somebody is told when it will not finish in time.
**Context:** `spec §R-28.b` and `R-28.d`, and T12's completion deadline. A partially scored portfolio presented as complete is worse than a late one presented as late.
**Read first:** `src/covenant_radar/scheduler/pipeline.py`, `spec §17.5` (T12).
**Contracts:** `C-20`; the alerts `T-145` wires.
**Files owned:** `src/covenant_radar/scheduler/policy.py`, `src/covenant_radar/services/nightly.py` (policy), `tests/integration/test_batch_resilience.py`
**Behaviour:** per-step retry with backoff; a run that cannot complete marked incomplete with the queue continuing to serve the last complete run and showing the data age; a deadline alert when T12 passes with the run still open; a per-borrower failure isolated so one bad borrower does not stop the book.
**Every case:** one borrower failing → isolated, recorded, the rest scored, and the failure surfaced in the run report and on the admin screen; the run still open at the deadline → alert raised, run continuing, and every affected screen showing its data age; a run abandoned → the last complete run still serving, with its age visible, never a blank queue; a step exhausting its retries → the run marked failed with the step named; the same failure recurring across nights → escalated, because a nightly failure that alerts identically every night stops being read.
**Steps:** 1. Implement per-borrower isolation with failure recording. 2. Implement per-step retry and exhaustion. 3. Implement the last-complete-run fallback with data-age display. 4. Implement the deadline alert. 5. Implement recurring-failure escalation.
**Tests:** `tests/integration/test_batch_resilience.py` — `test_single_borrower_failure_isolated`, `test_deadline_alert_raised_run_continues`, `test_queue_serves_last_complete_run_with_age`, `test_step_exhaustion_marks_run_failed_naming_step`, `test_recurring_failure_escalates`, `test_never_presents_partial_as_complete`.
**Run:** `pytest -q tests/integration/test_batch_resilience.py` 6 passed.
**Done when:** the six tests pass and a partial run is never presented as complete.
**Evidence:** a failure-isolation run report.

---

### T-123 · Connector framework and the transport protocol
`Milestone: M-5` · `Builds: R-29` · `Days: 2.0` · `Depends on: T-026, T-044` · `Snapshot: var/snapshots/T-123/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the bank's own systems become a data source, through one framework, **read-only by construction**.
**Context:** `spec §P-05` and `R-29.d`: no connector may write to a source system, and no write path exists in the codebase. The protocol has no write method, and a test proves the absence.
**Read first:** `plan.md §6` (`C-55`), `src/covenant_radar/ingestion/statements/`, `ingestion/signals/sources.py`.
**Contracts:** `C-55` `ConnectorTransport`; `C-19` connector administration.
**Files owned:** `src/covenant_radar/ports/connector.py`, `src/covenant_radar/ingestion/connectors/__init__.py`, `framework.py`, `mapping.py`, `src/covenant_radar/services/connectors.py`, `src/covenant_radar/db/repositories/connector.py`, `tests/unit/test_connector_framework.py`, `tests/security/test_connectors_read_only.py`
**Behaviour:** a transport protocol yielding source records; per-connector field mapping with a version; watermarking for incremental fetch; a run record with counts, lag and reconciliation; credentials from the secret store.
**Every case:** a transport with a write method → impossible, since the protocol declares none, and a scan test asserts no connector module contains a write, insert, update or delete call against a source; a mapping version change → a new version, and runs record which they used; a credential in a configuration file → refused by `T-004`'s rule; a run overlapping a previous run of the same connector → refused; a transport raising mid-stream → nothing committed, the run failed with the error.
**Steps:** 1. Define the read-only protocol. 2. Implement the framework: fetch, map, validate, quarantine, reconcile, record. 3. Implement mapping versioning. 4. Implement watermarking and the overlap guard. 5. Write the read-only scan test.
**Tests:** `tests/unit/test_connector_framework.py` — `test_protocol_has_no_write_method`, `test_mapping_version_recorded_per_run`, `test_overlapping_run_refused`, `test_transport_error_commits_nothing`; `tests/security/test_connectors_read_only.py` — `test_no_write_call_in_any_connector_module`, `test_credentials_only_from_secret_store`.
**Run:** `pytest -q tests/unit/test_connector_framework.py tests/security/test_connectors_read_only.py` 6 passed.
**Done when:** the six tests pass and the scan proves no write path exists.
**Evidence:** the scan output.

---

### T-124 · File-drop transport with decryption and mapping
`Milestone: M-5` · `Builds: R-29` · `Days: 2.0` · `Depends on: T-123` · `Snapshot: var/snapshots/T-124/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the transport most Indian bank extracts actually use — a scheduled drop of CSV, fixed-width or spreadsheet files, often encrypted.
**Context:** `spec §12.1`'s [OPEN-04] means the customer's exact layout may not be known yet; the transport is built against documented generic layouts and mapped at deployment, which is why mapping is configuration.
**Read first:** `src/covenant_radar/ingestion/connectors/framework.py`, `ingestion/statements/readers.py`.
**Contracts:** `C-55`.
**Files owned:** `src/covenant_radar/ingestion/connectors/file_drop.py`, `decrypt.py`, `layouts/*`, `tests/integration/test_file_drop.py`, `tests/fixtures/connectors/*`
**Behaviour:** watched directory with a stable-file check; optional decryption; CSV, fixed-width and spreadsheet readers; archive of processed files with their outcome; control-total reconciliation.
**Every case:** a file still being written → skipped until stable, because reading a half-written extract is how a portfolio gets half-loaded; a file whose control totals disagree with its rows → the run refused, the file archived as rejected, and the difference reported; a decryption failure → the file quarantined with the reason and never left decrypted on disk; the same file dropped twice → recognised by content hash and skipped with a note; a file matching no configured layout → quarantined naming the layouts tried.
**Steps:** 1. Implement the watched directory with the stability check. 2. Implement decryption with secure temporary handling. 3. Implement the three readers with layout selection. 4. Implement control-total reconciliation. 5. Implement archiving with outcomes and the duplicate check.
**Tests:** `tests/integration/test_file_drop.py` — `test_unstable_file_skipped_until_stable`, `test_control_total_mismatch_refuses_run`, `test_decryption_failure_quarantines_no_plaintext_left`, `test_duplicate_file_skipped_with_note`, `test_unmatched_layout_quarantined_naming_tried`, `test_processed_files_archived_with_outcome`.
**Run:** `pytest -q tests/integration/test_file_drop.py` 6 passed.
**Done when:** the six tests pass and no decrypted plaintext survives a failure.
**Evidence:** a reconciliation report, an archive listing.

---

### T-125 · REST pull transport with watermarking and pagination
`Milestone: M-5` · `Builds: R-29` · `Days: 1.5` · `Depends on: T-123` · `Snapshot: var/snapshots/T-125/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the transport for banks whose systems expose an API, resuming exactly where it stopped.
**Context:** `spec §R-29.b`: an incremental pull resumes from its watermark after failure with no gap and no duplication — the two failure modes that quietly corrupt a portfolio.
**Read first:** `src/covenant_radar/ingestion/connectors/framework.py`, `ai/providers/base.py` (the HTTP client pattern).
**Contracts:** `C-55`.
**Files owned:** `src/covenant_radar/ingestion/connectors/rest_pull.py`, `auth.py`, `tests/integration/test_rest_pull.py`
**Behaviour:** paginated fetch with a cursor or a timestamp watermark; configurable authentication from the secret store; rate-limit respect; resumption after failure.
**Every case:** a failure at page five of ten → resumption from page five with no gap and no duplication, proven by comparing the union against a full pull; a source returning an unstable ordering → detected by an overlap check and the run refused, because unstable ordering makes incremental pulls unsound; a rate-limit response → honoured with backoff, not retried immediately; an authentication failure → the run failed with the connector named and never the credential; a watermark that would move backwards → refused.
**Steps:** 1. Implement paginated fetch with both watermark styles. 2. Implement authentication from the secret store. 3. Implement rate-limit handling. 4. Implement resumption with the overlap check. 5. Guard against watermark regression.
**Tests:** `tests/integration/test_rest_pull.py` — `test_resumption_no_gap_no_duplication`, `test_unstable_ordering_detected_and_refused`, `test_rate_limit_honoured`, `test_auth_failure_names_connector_not_credential`, `test_watermark_never_regresses`.
**Run:** `pytest -q tests/integration/test_rest_pull.py` 5 passed.
**Done when:** the five tests pass and resumption is proven against a full pull.
**Evidence:** the resumption comparison.

---

### T-126 · Read-only database view transport
`Milestone: M-5` · `Builds: R-29` · `Days: 1.0` · `Depends on: T-123` · `Snapshot: var/snapshots/T-126/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the transport for banks that prefer to expose a view — the lowest-friction integration when the DBA is in the room.
**Context:** read-only is enforced by the connection itself: the connector requires credentials whose grants are read-only, and it verifies that at connection time rather than trusting configuration.
**Read first:** `src/covenant_radar/ingestion/connectors/framework.py`, `db/session.py`.
**Contracts:** `C-55`.
**Files owned:** `src/covenant_radar/ingestion/connectors/db_view.py`, `tests/integration/test_db_view.py`
**Behaviour:** a separate connection to the source with its own read-only credentials; incremental selection by watermark column; a startup verification that the credentials cannot write.
**Every case:** credentials with write grants → the connector refuses to run, naming the grant, because a read-only guarantee that depends on nobody having misconfigured it is not a guarantee; a view missing its watermark column → refused naming the column; a source transaction holding a long lock → the read times out and the run fails cleanly rather than blocking the host; a view whose shape changed → detected and refused naming the difference.
**Steps:** 1. Implement the separate connection with its own credentials. 2. Implement the grant verification at connection. 3. Implement watermark-based selection. 4. Implement shape-change detection. 5. Implement timeouts.
**Tests:** `tests/integration/test_db_view.py` — `test_write_grants_refuse_run_naming_grant`, `test_missing_watermark_column_refused`, `test_shape_change_refused_naming_difference`, `test_read_timeout_fails_cleanly`.
**Run:** `pytest -q tests/integration/test_db_view.py` 4 passed.
**Done when:** the four tests pass and write-capable credentials are refused.
**Evidence:** the grant-verification transcript.

---

### T-127 · Reconciliation, dry run and schema-change refusal
`Milestone: M-5` · `Builds: R-29` · `Days: 1.5` · `Depends on: T-124` · `Snapshot: var/snapshots/T-127/` · `Build: DEFERRED — out of scope for this window`

**Goal:** an administrator can see exactly what a connector would do before enabling it, and a source that changes shape stops the run instead of loading wrong data.
**Context:** `spec §R-29.c`: a source schema change is detected and the run refused with the difference named. Loading wrong data silently is the worst outcome available to an ingestion system.
**Read first:** `src/covenant_radar/ingestion/connectors/framework.py`, `services/connectors.py`.
**Contracts:** `C-19` dry run.
**Files owned:** `src/covenant_radar/ingestion/connectors/reconcile.py`, `schema_guard.py`, `src/covenant_radar/services/connectors.py` (dry run), `web/templates/screens/admin/connectors/*`, `tests/integration/test_connector_reconciliation.py`
**Behaviour:** a recorded source schema fingerprint per connector; comparison on every run; a dry run reporting counts, samples, mappings applied and rejects **without writing anything**; a reconciliation report against control totals.
**Every case:** a dry run → nothing written anywhere, asserted by comparing row counts across every table before and after; a new column appearing in the source → reported and permitted if the mapping ignores it, refused if the mapping is ambiguous, because a silently ignored column may be the one that matters; a column removed that the mapping uses → refused naming it; a type change → refused naming both types; the fingerprint updated only by an explicit administrator action, recorded.
**Steps:** 1. Implement the schema fingerprint and comparison. 2. Implement the additive-versus-breaking distinction. 3. Implement dry run with a hard no-write guarantee. 4. Implement the reconciliation report. 5. Implement fingerprint acceptance as a recorded action.
**Tests:** `tests/integration/test_connector_reconciliation.py` — `test_dry_run_writes_nothing_anywhere`, `test_new_ignored_column_permitted_and_reported`, `test_removed_used_column_refused`, `test_type_change_refused_naming_both`, `test_fingerprint_update_is_recorded_action`, `test_reconciliation_against_control_totals`.
**Run:** `pytest -q tests/integration/test_connector_reconciliation.py` 6 passed.
**Done when:** the six tests pass and a dry run provably writes nothing.
**Evidence:** a dry-run report, a refused schema change.

---

### [x] T-128 · Feed adapter protocol and the synthetic generator
`Milestone: M-5` · `Builds: R-30` · `Days: 1.5` · `Depends on: T-042` · `Snapshot: var/snapshots/T-128/` · `Build: implemented out-of-order this session, pulled forward from §2.4's deferred list at explicit request; not run through the numbered §2.3 build order or python -m radarctl gate (git and the AI provider key are unavailable in this environment)`

**Goal:** external signals behind one interface, with a synthetic source so the capability works and is testable before any subscription exists.
**Context:** `spec §12.1`'s [OPEN-05]: the licensed feed may not be procured yet. The synthetic generator is the documented default and is also what the evaluation and reference portfolios use.
**Read first:** `plan.md §6` (`C-56`), `src/covenant_radar/ingestion/signals/sources.py`, `evaluation/reference_portfolio/signals.py`.
**Contracts:** `C-56` `FeedAdapter`.
**Files owned:** `src/covenant_radar/ports/feed.py`, `src/covenant_radar/ingestion/feeds/__init__.py`, `framework.py`, `synthetic.py`, `src/covenant_radar/services/feeds.py`, `tests/unit/test_feed_protocol.py`, `tests/integration/test_synthetic_feed.py`
**Behaviour:** an adapter yielding items with source, published time, title, body, entities and a source reference; a poll cycle with a per-source watermark; the synthetic generator producing a deterministic, plausible stream for the reference portfolio.
**Every case:** an adapter yielding an item with no resolvable entity → passed to resolution, which decides, rather than being dropped at the adapter; a feed unconfigured → the capability reports so and that evidence family is absent with a reason, never silently empty; the synthetic generator run twice with the same seed → identical; an adapter raising → that feed degraded alone, others unaffected; an item older than the retention horizon → ignored with a count, not stored.
**Steps:** 1. Define the protocol and the item shape. 2. Implement the poll framework with watermarks and per-source isolation. 3. Implement the synthetic generator deterministically. 4. Report the capability state. 5. Isolate failures per source.
**Tests:** `tests/unit/test_feed_protocol.py` — `test_item_shape`, `test_unconfigured_feed_reports_absence_with_reason`, `test_adapter_failure_isolated`; `tests/integration/test_synthetic_feed.py` — `test_deterministic_with_seed`, `test_stream_covers_industry_and_news`, `test_stale_items_ignored_with_count`.
**Run:** `pytest -q tests/unit/test_feed_protocol.py tests/integration/test_synthetic_feed.py` 6 passed.
**Done when:** the six tests pass and one failing feed cannot affect another.
**Evidence:** a synthetic stream sample.

---

### T-129 · News, industry and bureau adapters
`Milestone: M-5` · `Builds: R-30` · `Days: 1.5` · `Depends on: T-128` · `Snapshot: var/snapshots/T-129/` · `Build: DEFERRED — out of scope for this window`

**Goal:** three real adapters, built and tested against recorded fixtures so they are ready the day a subscription arrives.
**Context:** built against documented API shapes with recorded fixtures, since the live subscription is [OPEN-05]. The adapters are complete; only the credential is outstanding.
**Read first:** `src/covenant_radar/ingestion/feeds/framework.py`, `ai/providers/base.py` (the HTTP client pattern).
**Contracts:** `C-56`.
**Files owned:** `src/covenant_radar/ingestion/feeds/adapters/news.py`, `industry.py`, `bureau.py`, `tests/integration/test_feed_adapters.py`, `tests/fixtures/feeds/*`
**Behaviour:** each adapter mapping its source's response to the item shape, handling pagination, rate limits and authentication from the secret store, and recording per-call cost or quota where the source reports it.
**Every case:** a rate limit → honoured with backoff and the wait recorded, never hammered; a malformed item in an otherwise good page → skipped with a count, the page continuing; an authentication failure → that feed disabled with an administrator alert, others unaffected; a source returning items outside the requested window → filtered with a count, because trusting a source's filtering is how a watermark drifts; cost or quota reported → recorded for the administrator's cost view.
**Steps:** 1. Implement the three adapters over one HTTP client. 2. Implement pagination, rate limits and authentication. 3. Implement per-item validation with skip counts. 4. Implement window filtering. 5. Record cost and quota. 6. Build recorded fixtures for each.
**Tests:** `tests/integration/test_feed_adapters.py` — `test_each_adapter_maps_fixture_to_item_shape`, `test_rate_limit_honoured_and_recorded`, `test_malformed_item_skipped_with_count`, `test_auth_failure_disables_only_that_feed`, `test_out_of_window_items_filtered_with_count`, `test_cost_recorded_where_reported`.
**Run:** `pytest -q tests/integration/test_feed_adapters.py` 6 passed.
**Done when:** the six tests pass against recorded fixtures with no network access.
**Evidence:** the fixture-based test output.

---

### T-130 · Entity resolution, review queue, negative-match memory
`Milestone: M-5` · `Builds: R-30` · `Days: 2.0` · `Depends on: T-129` · `Snapshot: var/snapshots/T-130/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the hard half of external signals — deciding whether a story about "Meridian Auto" is about *this* borrower, without asserting it when unsure.
**Context:** `spec §R-30.b` and T10's confidence floors: auto-accept above the upper floor, discard below the lower, and queue the band between for a human. `spec §R-30.b`: an ambiguous match enters the review queue rather than being asserted.
**Read first:** `spec §17.5` (T10), `plan.md §5.2` (`borrower`, `cin_fingerprint`), `src/covenant_radar/ingestion/feeds/framework.py`.
**Contracts:** the matches `T-131` consumes; the review queue `T-115` shows.
**Files owned:** `src/covenant_radar/ingestion/feeds/entity_resolution.py`, `src/covenant_radar/services/entity_matching.py`, `src/covenant_radar/web/routes/entity_matching.py`, `web/templates/screens/admin/matching/*`, `tests/unit/test_entity_resolution.py`, `tests/integration/test_match_review.py`
**Behaviour:** candidate generation from name, identifier, group and industry; scoring into a confidence; T10 applied; a review queue for the middle band; a negative-match memory so a rejected pairing is never re-proposed.
**Every case:** a confidence above the upper floor → auto-accepted with the score recorded; below the lower floor → discarded with a count, not stored as a near miss; between → queued, and the item held rather than becoming evidence until decided; a pairing rejected by a reviewer → recorded negatively and never proposed again, because re-asking a question a human already answered is how a review queue becomes ignored; two borrowers in one group both matching → both proposed with the group relationship shown, never silently choosing one; a match accepted then found wrong → reversible, with the evidence it created superseded rather than deleted.
**Steps:** 1. Implement candidate generation and scoring. 2. Apply T10's two floors. 3. Implement the review queue with accept and reject. 4. Implement the negative-match memory. 5. Implement reversal with evidence supersession. 6. Handle group ambiguity.
**Tests:** `tests/unit/test_entity_resolution.py` — `test_above_upper_floor_auto_accepted`, `test_below_lower_floor_discarded_with_count`, `test_middle_band_queued_and_item_held`, `test_group_ambiguity_proposes_both`; `tests/integration/test_match_review.py` — `test_rejected_pairing_never_reproposed`, `test_reversal_supersedes_evidence`.
**Run:** `pytest -q tests/unit/test_entity_resolution.py tests/integration/test_match_review.py` 6 passed.
**Done when:** the six tests pass and no ambiguous match becomes evidence without a decision.
**Evidence:** a review queue with accepted, rejected and pending matches.

---

### T-131 · Feed deduplication, relevance and cost accounting
`Milestone: M-5` · `Builds: R-30` · `Days: 1.0` · `Depends on: T-130` · `Snapshot: var/snapshots/T-131/` · `Build: DEFERRED — out of scope for this window`

**Goal:** one story reported by three sources becomes one evidence item, and the customer can see what the feeds cost.
**Context:** `spec §R-30.c`: the same story from two sources deduplicates to one evidence item citing both. Otherwise a widely reported event triples its own weight, which is exactly the noise amplification the ledger exists to prevent.
**Read first:** `src/covenant_radar/ingestion/feeds/entity_resolution.py`, `domain/signals/evidence.py`.
**Contracts:** the evidence items `T-046` derives.
**Files owned:** `src/covenant_radar/ingestion/feeds/dedupe.py`, `relevance.py`, `cost.py`, `tests/integration/test_feed_dedupe.py`
**Behaviour:** near-duplicate detection across sources within a time window; relevance classification against the borrower's context; one evidence item per deduplicated story citing every source; per-feed cost and quota accounting.
**Every case:** the same story from three sources → one evidence item citing all three, and a test asserting the magnitude is not tripled; two genuinely distinct events on the same day → not merged, and the discriminator recorded; an irrelevant story that mentions the borrower → classified as low relevance and recorded without contributing pressure, never discarded, because relevance judgements should be reviewable; a source added later reporting an already-recorded story → merged into the existing item, adding a citation; cost accumulating past a configured budget → alerted.
**Steps:** 1. Implement near-duplicate detection with a windowed similarity. 2. Implement relevance classification with the recorded reason. 3. Merge into one evidence item with multiple citations. 4. Implement cost and quota accounting with a budget alert. 5. Assert magnitude is not multiplied by duplication.
**Tests:** `tests/integration/test_feed_dedupe.py` — `test_three_sources_one_item_three_citations`, `test_magnitude_not_multiplied`, `test_distinct_same_day_events_not_merged`, `test_low_relevance_recorded_not_discarded`, `test_late_source_merges_and_adds_citation`, `test_budget_alert`.
**Run:** `pytest -q tests/integration/test_feed_dedupe.py` 6 passed.
**Done when:** the six tests pass and duplication cannot inflate an item's weight.
**Evidence:** a deduplicated item with three citations.

---

### [x] `T-132` · CRILC export and the weekly default report
`Milestone: M-5` · `Builds: R-31` · `Days: 2.0` · `Depends on: T-036, T-056` · `Snapshot: var/snapshots/T-132/` · `Build: DONE — implemented ahead of schedule; see tests/integration/test_crilc.py`

**Goal:** the supervisory return the bank already has to file, produced from the same records the warnings came from.
**Context:** `spec §2.1`'s Prudential Framework: CRILC monthly plus weekly default reports for exposures at or above ₹5 crore. `spec §R-31.a`: the export validates against the published layout and reconciles to source records.
**Read first:** `spec §2.1`, `src/covenant_radar/domain/covenants/sma.py`, `plan.md §5.2`.
**Contracts:** the report resources; `C-60`.
**Files owned:** `src/covenant_radar/reporting/__init__.py`, `crilc.py`, `layouts/crilc/*`, `src/covenant_radar/services/reporting.py`, `tests/integration/test_crilc.py`
**Behaviour:** a monthly CRILC extract and a weekly default report in the published layouts, generated for a stated as-of date from stored records, with a reconciliation summary and a retained generation record.
**Every case:** a facility below the reporting threshold → excluded, and the exclusion counted so the reconciliation adds up; a borrower with missing data required by the layout → listed in an exceptions section rather than omitted or defaulted, because a return with a silently defaulted field is a misstatement; a regeneration for a past date → reproducing the original exactly for all non-timestamp content, proven by hash; a layout version change → a new layout version with both retained; the generation → audited with the parameters and the row count.
**Steps:** 1. Encode the layouts as versioned data. 2. Implement extraction from stored records for a stated as-of date. 3. Implement the exceptions section for incomplete records. 4. Implement reconciliation and the generation record. 5. Assert reproducibility by hash.
**Tests:** `tests/integration/test_crilc.py` — `test_validates_against_layout`, `test_below_threshold_excluded_and_counted`, `test_incomplete_record_listed_not_defaulted`, `test_regeneration_reproduces_exactly`, `test_layout_version_change_retains_both`, `test_generation_audited`.
**Run:** `pytest -q tests/integration/test_crilc.py` 6 passed.
**Done when:** the six tests pass and a regeneration reproduces the original.
**Evidence:** a generated export with its reconciliation.

---

### [x] `T-133` · EWS/RFA pack assembly
`Milestone: M-5` · `Builds: R-31` · `Days: 1.5` · `Depends on: T-068` · `Snapshot: var/snapshots/T-133/` · `Build: DONE — implemented ahead of schedule; see tests/integration/test_rfa_pack.py`

**Goal:** the pack an RFA committee needs, assembled from the trail rather than compiled by an analyst over two days.
**Context:** `spec §2.1`'s Fraud Directions. The product's role is to supply the evidence; the classification remains a human, regulated act (`spec §P-02`), and the pack says so on its face.
**Read first:** `src/covenant_radar/audit/reconstruct.py`, `services/reporting.py`, `spec §P-02`.
**Contracts:** the pack export.
**Files owned:** `src/covenant_radar/reporting/rfa_pack.py`, `src/covenant_radar/web/templates/exports/rfa_pack.html`, `tests/integration/test_rfa_pack.py`
**Behaviour:** a per-borrower pack containing exposure and facility summary, covenant position and history, the signal and evidence timeline with sources, the forecast history with drivers, every warning raised with its disposition, interventions taken, documents and certificates, and the audit trail summary — exportable as one bundle.
**Every case:** a pack for a borrower with a short history → produced with the gaps named and dated, never padded; a pack containing a model-drafted memo → the memo's drafted sections marked as such, so a committee is never reading generated prose believing it computed; the pack's cover carrying the advisory-only statement and the explicit note that it contains no fraud determination; a purged element → named with its retention rule; the export audited with who assembled it and for whom.
**Steps:** 1. Assemble the sections from the reconstruction and the record set. 2. Render with the cover statement. 3. Mark model-drafted content. 4. Handle gaps and purged elements explicitly. 5. Export as a bundle and audit it.
**Tests:** `tests/integration/test_rfa_pack.py` — `test_every_section_present`, `test_gaps_named_and_dated`, `test_model_drafted_content_marked`, `test_cover_carries_advisory_and_no_fraud_determination`, `test_purged_element_named_with_rule`, `test_export_audited`.
**Run:** `pytest -q tests/integration/test_rfa_pack.py` 6 passed.
**Done when:** the six tests pass and the pack cannot be mistaken for a fraud determination.
**Evidence:** a rendered pack.

---

### [x] `T-134` · Board MIS and scheduled report delivery
`Milestone: M-5` · `Builds: R-31` · `Days: 1.0` · `Depends on: T-132, T-117` · `Snapshot: var/snapshots/T-134/` · `Build: DONE`

**Goal:** the portfolio view a credit committee reads monthly, generated and delivered on a schedule.
**Context:** `spec §R-31`. The MIS is also where G1, G3 and G6 become visible to the customer's own management, which is what turns a pilot's measurements into an ongoing conversation.
**Read first:** `src/covenant_radar/services/reporting.py`, `notifications/digest.py`, `spec §6`.
**Contracts:** the report resources.
**Files owned:** `src/covenant_radar/reporting/mis.py`, `src/covenant_radar/web/templates/exports/mis.html`, `src/covenant_radar/scheduler/jobs.py` (the report job), `tests/integration/test_mis.py`
**Behaviour:** portfolio distribution by band and SMA, migration between periods, early-warning lead time achieved, escalation and disposition statistics, model performance from the evaluation record, and connector and data-quality summaries; scheduled generation and delivery to a distribution list.
**Every case:** a period with no data → the section states so rather than rendering an empty chart, because an empty chart reads as a zero; a metric that cannot be computed for the period → named with the reason; a scheduled delivery failing → retried and, on exhaustion, surfaced to the administrator rather than silently missed; a report regenerated → reproducing the original; every chart accompanied by its figures, per the design rule.
**Steps:** 1. Compute the metric set for a period. 2. Render with figures beside every chart. 3. Handle absent data explicitly. 4. Schedule generation and delivery. 5. Assert reproducibility.
**Tests:** `tests/integration/test_mis.py` — `test_every_section_computed`, `test_absent_data_stated_not_charted_as_zero`, `test_uncomputable_metric_named_with_reason`, `test_delivery_failure_surfaced`, `test_regeneration_reproduces`, `test_every_chart_has_its_figures`.
**Run:** `pytest -q tests/integration/test_mis.py` 6 passed.
**Done when:** the six tests pass and no chart renders without its figures.
**Evidence:** a generated MIS.

---

### [x] `T-135` · REST API resources, schemas and error envelope
`Milestone: M-5` · `Builds: R-32` · `Days: 2.0` · `Depends on: T-016` · `Snapshot: var/snapshots/T-135/` · `Build: DONE — pulled forward ahead of schedule, see MERGE_LOG.md`

**Goal:** the bank's own systems can read what the product knows, under the same permissions and scope as a person.
**Context:** `spec §R-32`. Read-heavy by design: the write surface is limited to ingestion, disposition, case update and simulation, and never to anything `spec §8.2` forbids.
**Read first:** `plan.md §6` (`C-21`, `C-22`), `src/covenant_radar/api/deps.py`, every service.
**Contracts:** `C-21`, `C-22`.
**Files owned:** `src/covenant_radar/api/v1/routers/*`, `api/v1/schemas/*`, `api/errors.py`, `api/pagination.py`, `tests/integration/test_api_resources.py`
**Behaviour:** resources for portfolio, borrower, facility, covenant, test, evidence, forecast, driver, simulation, memo, case and audit event; cursor pagination; filtering; conditional requests; one error envelope with a code, a message and a field path.
**Every case:** a write attempt on a read-only resource → `405` with the permitted methods; an endpoint that would approve a credit action → does not exist, asserted by enumerating the route table against the forbidden operations; a scoped key requesting another portfolio → `404`; a cursor from a different filter set → refused rather than silently reinterpreted; a conditional request with a matching version → `304`; every error the same envelope, asserted across every route.
**Steps:** 1. Write the schemas from the service view models. 2. Write the routers with permission declarations. 3. Implement cursor pagination and filtering. 4. Implement conditional requests. 5. Implement the single error envelope. 6. Assert the forbidden operations have no route.
**Tests:** `tests/integration/test_api_resources.py` — `test_every_resource_lists_and_reads`, `test_write_on_read_only_405`, `test_no_route_for_forbidden_operations`, `test_scope_returns_404`, `test_mismatched_cursor_refused`, `test_conditional_request_304`, `test_single_error_envelope_across_routes`.
**Run:** `pytest -q tests/integration/test_api_resources.py` 7 passed.
**Done when:** the seven tests pass and no route exists for a forbidden operation.
**Evidence:** the route table with permissions.

---

### [x] `T-136` · API keys, scoping, rate limits, OpenAPI and contract tests
`Milestone: M-5` · `Builds: R-32` · `Days: 2.0` · `Depends on: T-135` · `Snapshot: var/snapshots/T-136/` · `Build: DONE`

**Goal:** the API is documented, versioned, rate-limited and provably matching its own specification.
**Context:** `spec §R-32.a`: the OpenAPI document validates and matches the implementation, verified by contract tests. A specification that drifts from its implementation is worse than none, because integrators trust it.
**Read first:** `src/covenant_radar/api/v1/routers/`, `security/ratelimit.py`, `db/scoping.py`.
**Contracts:** `C-21`, `C-23`.
**Files owned:** `src/covenant_radar/api/openapi.py`, `api/keys.py`, `src/covenant_radar/services/api_keys.py`, `docs/api/*`, `tests/contract/test_api_contract.py`, `tests/security/test_api_keys.py`
**Behaviour:** key issue, scoping, rotation and revocation with the key shown once; per-key rate limits; a generated OpenAPI document with examples; contract tests in both directions; a documented deprecation policy.
**Every case:** a key scoped to one portfolio → cannot read another's data through any endpoint, asserted per endpoint; a revoked key → refused immediately, not at next cache expiry; a rate limit reached → `429` with `Retry-After`, and the event recorded; the OpenAPI document disagreeing with the implementation in either direction → a contract-test failure; a key's plaintext → never retrievable after issue and never in a log, asserted by a scan.
**Steps:** 1. Implement key issue, hashing, scoping, rotation and revocation. 2. Implement per-key rate limiting. 3. Generate the OpenAPI document with examples. 4. Write bidirectional contract tests. 5. Write the deprecation policy. 6. Assert no key material is logged.
**Tests:** `tests/contract/test_api_contract.py` — `test_document_validates`, `test_implementation_matches_document`, `test_document_matches_implementation`; `tests/security/test_api_keys.py` — `test_scoped_key_cannot_cross_portfolio_on_any_endpoint`, `test_revocation_immediate`, `test_rate_limit_429_with_retry_after`, `test_key_material_never_logged`.
**Run:** `pytest -q tests/contract/test_api_contract.py tests/security/test_api_keys.py` 7 passed.
**Done when:** the seven tests pass and the document cannot drift from the implementation.
**Evidence:** the OpenAPI document, the contract-test output.

---

### [x] T-137 · Search across entities with scope enforcement
`Milestone: M-5` · `Builds: R-33` · `Days: 1.5` · `Depends on: T-016` · `Snapshot: var/snapshots/T-137/` · `Build: DONE — implemented ahead of schedule; see tests/integration/test_search.py`

**Goal:** find anything quickly, and never find anything the caller may not see.
**Context:** `spec §R-33.a`: search results never include a record outside the caller's scope. Search is the classic scope-leak surface because it crosses every entity at once.
**Read first:** `src/covenant_radar/db/scoping.py`, every repository.
**Contracts:** the search resource.
**Files owned:** `src/covenant_radar/services/search.py`, `src/covenant_radar/db/repositories/search.py`, `src/covenant_radar/web/routes/search.py`, `web/templates/screens/search/*`, `tests/integration/test_search.py`, `tests/security/test_search_scope.py`
**Behaviour:** full-text and structured search across borrowers, facilities, covenants, documents, memos, cases and audit events, scoped, ranked, with type filters and highlighted matches.
**Every case:** a term matching an out-of-scope record → absent from results and from the count, because a count that includes hidden records is itself a disclosure; a term matching a personal-class value → matched only for a caller permitted to see it, and the access logged; a document body match → the snippet drawn from the permitted portion only; an empty query → the recent-items list, not an error; search over the reference portfolio returning inside the latency budget.
**Steps:** 1. Implement per-entity search with the scope predicate. 2. Implement ranking and type filters. 3. Implement snippets with permission-aware extraction. 4. Ensure counts respect scope. 5. Add the performance test.
**Tests:** `tests/integration/test_search.py` — `test_all_entity_types_searchable`, `test_empty_query_shows_recent`, `test_snippets_permission_aware`, `test_within_latency_budget`; `tests/security/test_search_scope.py` — `test_out_of_scope_absent_from_results_and_count`, `test_personal_match_requires_permission_and_is_logged`.
**Run:** `pytest -q tests/integration/test_search.py tests/security/test_search_scope.py` 6 passed.
**Done when:** the six tests pass and counts respect scope.
**Evidence:** the scope-leak test output.

---

### [x] T-138 · Saved views, recent items and sharing
`Milestone: M-5` · `Builds: R-33` · `Days: 1.0` · `Depends on: T-137, T-074` · `Snapshot: var/snapshots/T-138/` · `Build: DONE — implemented`

**Goal:** a user's own slices persist and can be shared, without sharing becoming a disclosure.
**Context:** `spec §R-33.b`. A shared view is a filter, not a result set: it applies within the recipient's scope, which is what makes sharing safe.
**Read first:** `src/covenant_radar/domain/triage/views.py`, `web/routes/queue.py`.
**Contracts:** the saved-view resources.
**Files owned:** `src/covenant_radar/services/views.py`, `src/covenant_radar/db/repositories/view.py`, `src/covenant_radar/web/routes/views.py`, `tests/integration/test_saved_views.py`
**Behaviour:** named views over queue, case and search filters; a recent-items list per user; sharing to a user or a role, applying within the recipient's scope; a default view per user.
**Every case:** a view shared to a user with a narrower scope → applied within theirs, returning fewer rows and saying so, never widening it; a view whose owner is deactivated → retained for its recipients with the ownership transferred to an administrator; a view referencing a deleted filter target → loading with that filter dropped and the user told; a recent-items entry the user has lost access to → removed silently, since it is a convenience and not a record.
**Steps:** 1. Implement view storage and application. 2. Implement sharing with recipient-scope application. 3. Implement recent items with access filtering. 4. Handle owner deactivation and dangling filters. 5. Implement the default view.
**Tests:** `tests/integration/test_saved_views.py` — `test_shared_view_applies_within_recipient_scope`, `test_deactivated_owner_view_retained_and_transferred`, `test_dangling_filter_dropped_with_notice`, `test_recent_items_filtered_by_access`, `test_default_view_applied_on_entry`.
**Run:** `pytest -q tests/integration/test_saved_views.py` 5 passed.
**Done when:** the five tests pass and a shared view never widens a recipient's scope.
**Evidence:** the sharing scope demonstration.

---

### [x] T-139 · Bulk operations and asynchronous export
`Milestone: M-5` · `Builds: R-34` · `Days: 1.5` · `Depends on: T-074, T-120` · `Snapshot: var/snapshots/T-139/` · `Build: DONE — implemented`

**Goal:** a desk can act on fifty cases at once and export a portfolio without the browser timing out.
**Context:** `spec §R-34.a`: a bulk assignment writes one audit event per affected case plus one summary event, so neither the detail nor the shape of the action is lost.
**Read first:** `src/covenant_radar/services/cases.py`, `scheduler/jobs.py`, `audit/record.py`.
**Contracts:** the bulk and export resources.
**Files owned:** `src/covenant_radar/services/bulk.py`, `src/covenant_radar/services/export.py`, `src/covenant_radar/web/routes/bulk.py`, `web/templates/screens/exports/*`, `tests/integration/test_bulk_operations.py`
**Behaviour:** multi-select assignment, state change, watchlist and export; asynchronous export for large sets with notification and a download link; per-item and summary auditing; partial success reported per item.
**Every case:** a bulk operation where some items fail → the successes applied, the failures listed per item with reasons, and nothing silently skipped; an export exceeding the synchronous threshold → queued, with the user notified on completion and the link expiring per policy; an export including an out-of-scope row → impossible, since the export runs under the requester's scope, asserted by a test; a bulk action on an item the user may not change → excluded before execution with the reason; the summary event recording the filter, the count and the outcome distribution.
**Steps:** 1. Implement bulk operations with per-item permission checks and partial success. 2. Implement synchronous and asynchronous export paths. 3. Implement the expiring download link. 4. Write per-item and summary audit events. 5. Run exports under the requester's scope.
**Tests:** `tests/integration/test_bulk_operations.py` — `test_partial_success_reported_per_item`, `test_unpermitted_items_excluded_with_reason`, `test_large_export_queued_and_notified`, `test_export_scoped_to_requester`, `test_per_item_and_summary_events`, `test_download_link_expires`.
**Run:** `pytest -q tests/integration/test_bulk_operations.py` 6 passed.
**Done when:** the six tests pass and no bulk item fails silently.
**Evidence:** a bulk operation report with partial success.

---

### T-140 · Translation catalogues, extraction and the build check

> **REMOVED — do not build.** This task is struck from the product because the catalogue machinery exists only to serve the Hindi feature. Nothing in §2.3 depends on it. The block is retained below only so the decision is auditable.
`Milestone: M-5` · `Builds: R-35` · `Days: 1.5` · `Depends on: T-022` · `Snapshot: var/snapshots/T-140/` · `Build: REMOVED — not built`

**Goal:** every user-facing string is externalised and translatable, enforced by the build so it stays that way.
**Context:** `spec §R-35.a`: a literal string in a template fails the build. The scaffold has existed since `T-022`; this task completes extraction, the catalogue workflow and the enforcement.
**Read first:** `src/covenant_radar/i18n/`, every template and view module.
**Contracts:** none.
**Files owned:** `src/covenant_radar/i18n/catalogues/en/*`, `extract.py`, `src/covenant_radar/cli.py` (the i18n group), `tests/unit/test_i18n.py`
**Behaviour:** extraction from templates, view models, error messages, notification templates and enumerated reason texts into catalogues; a build check failing on a literal or a missing key; pluralisation and context support.
**Every case:** a literal user-facing string anywhere → the build fails naming the file and line; a key present in a template but missing from a shipped catalogue → the build fails; a key in the catalogue no longer used → reported for removal, not failed, since removing it is a separate decision; a plural form → handled with the language's own rules, not by string concatenation; an enumerated reason from the domain → translated at render, never stored translated, because the stored record must stay language-neutral.
**Steps:** 1. Implement extraction across every source of user-facing text. 2. Build the English catalogue as the source language. 3. Implement the literal and missing-key checks and add them to the gate. 4. Implement pluralisation and context. 5. Ensure domain reasons are translated at render only.
**Tests:** `tests/unit/test_i18n.py` — `test_literal_string_fails_build`, `test_missing_key_fails_build`, `test_unused_key_reported_not_failed`, `test_plural_forms_handled`, `test_domain_reasons_translated_at_render_only`, `test_every_notification_template_externalised`.
**Run:** `pytest -q tests/unit/test_i18n.py` 6 passed · `radarctl i18n extract` produces no new untranslated keys.
**Done when:** the six tests pass and no literal user-facing string remains.
**Evidence:** the extraction report.

---

### T-141 · Hindi translation and locale formatting

> **REMOVED — do not build.** This task is struck from the product because the Hindi feature is removed from the product. Nothing in §2.3 depends on it. The block is retained below only so the decision is auditable.
`Milestone: M-5` · `Builds: R-35` · `Days: 1.0` · `Depends on: T-140` · `Snapshot: var/snapshots/T-141/` · `Build: REMOVED — not built`

**Goal:** the second shipped language, complete, with Indian conventions correct in both.
**Context:** `spec §R-35.b`: switching to Hindi renders every screen with no untranslated key and no layout overflow. Devanagari is wider and taller than Latin, so this is a layout test as much as a translation task.
**Read first:** `src/covenant_radar/i18n/catalogues/en/`, `web/static/css/tokens.css` (the Devanagari stack).
**Contracts:** none.
**Files owned:** `src/covenant_radar/i18n/catalogues/hi/*`, `src/covenant_radar/i18n/formatting.py` (locale rules), `tests/e2e/test_hindi.py`
**Behaviour:** a complete Hindi catalogue; the Devanagari face applied for Hindi content; Indian number, currency, date and quarter formatting correct in both locales; a per-user preference.
**Every case:** any untranslated key in Hindi → the build fails; a Devanagari string overflowing its container on any screen → the layout test fails, and the fix is a layout change rather than a shortened translation; numbers rendering in lakh and crore in both locales; a mixed-script string, such as an English entity name inside Hindi prose → rendering correctly with the right face per run; a locale-specific date format → correct and tested at a month boundary.
**Steps:** 1. Translate the catalogue completely. 2. Apply the Devanagari stack for Hindi content. 3. Implement locale formatting for both. 4. Write the overflow test across every screen in Hindi. 5. Test mixed-script rendering.
**Tests:** `tests/e2e/test_hindi.py` — `test_no_untranslated_key`, `test_no_overflow_on_any_screen`, `test_lakh_crore_in_both_locales`, `test_mixed_script_renders_correctly`, `test_date_and_quarter_formats`.
**Run:** `pytest -q tests/e2e/test_hindi.py` 5 passed · screenshots of every screen in Hindi.
**Done when:** the five tests pass and no screen overflows in Hindi. 
**Evidence:** the Hindi screenshots, the M-5 gate record.

---

### M-6 · Hardening and release — 55.0 days

*Requirement grouping, not build order — see §2.3.*

---

### [x] `T-142` · Logging finalisation: redaction, sampling, rotation, retention
`Milestone: M-6` · `Builds: N-02` · `Days: 1.5` · `Depends on: T-005` · `Snapshot: var/snapshots/T-142/` · `Build: DONE`

**Goal:** logs that answer questions an hour later without ever containing something they should not.
**Context:** `spec §20`: never logged are secrets, personal-class values in the clear, full document or clause bodies, and model prompt bodies. `spec §2.1`'s CERT-In direction requires 180-day in-country retention with integrity.
**Read first:** `src/covenant_radar/observability/logging.py`, `spec §20`, `config/logging.toml`.
**Contracts:** the log streams `T-145`'s alerts and `T-164`'s runbook reference.
**Files owned:** `src/covenant_radar/observability/logging.py` (completion), `redaction.py`, `retention.py`, `config/logging.toml`, `tests/unit/test_log_redaction.py`, `tests/security/test_log_contents.py`
**Behaviour:** structured JSON with the request or job id on every line; a redaction processor over configured patterns and personal-class field names; per-logger sampling; size and time rotation with an integrity hash per rotated file; 180-day default retention.
**Every case:** a personal-class value passed to a log call → redacted to a token before writing, and a test asserts it across a full workload rather than a single call; a secret pattern → redacted; a prompt body → rejected outright by the application logger, since prompts belong only in the model-call stream; the log directory unwritable → the request still completes and the failure surfaces on the health view, never swallowed and never fatal; a rotated file → hashed, with the hash retained so tampering is detectable.
**Steps:** 1. Complete the redaction processor over patterns and field names. 2. Implement sampling per logger. 3. Implement rotation with per-file integrity hashes. 4. Implement retention with the CERT-In default. 5. Implement the unwritable-directory path with health surfacing. 6. Run a full-workload scan.
**Tests:** `tests/unit/test_log_redaction.py` — `test_personal_field_names_redacted`, `test_secret_patterns_redacted`, `test_prompt_body_rejected`, `test_rotation_hashes_file`; `tests/security/test_log_contents.py` — `test_full_workload_logs_contain_no_personal_value`, `test_full_workload_logs_contain_no_secret`, `test_unwritable_directory_does_not_break_request`.
**Run:** `pytest -q tests/unit/test_log_redaction.py tests/security/test_log_contents.py` 7 passed.
**Done when:** the seven tests pass and a full-workload log scan is clean.
**Evidence:** the scan report.

---

### [x] `T-143` · Metrics, health, readiness and version endpoints
`Milestone: M-6` · `Builds: N-02` · `Days: 1.5` · `Depends on: T-142` · `Snapshot: var/snapshots/T-143/` · `Build: DONE`

**Goal:** the numbers an operator watches, and the endpoints a load balancer and a human both need.
**Context:** `spec §20`'s metric list. Health is not readiness: a process that is up but cannot reach its database is healthy and not ready, and conflating them causes outages during deployment.
**Read first:** `spec §20`, `src/covenant_radar/config/capabilities.py`, `db/session.py`.
**Contracts:** `C-23`.
**Files owned:** `src/covenant_radar/observability/metrics.py`, `health.py`, `src/covenant_radar/web/routes/system.py`, `tests/integration/test_observability_endpoints.py`
**Behaviour:** every metric in `spec §20` exported; `/health` reporting process liveness; `/ready` checking database, document store, scheduler and configured capabilities; `/version` reporting the version, the commit and the build time; `/metrics` restricted by network or token.
**Every case:** the database unreachable → healthy but not ready, with the failing check named; a capability unconfigured → reported as not-configured, which is not a readiness failure, because an unconfigured optional feature is a choice; `/metrics` requested without authorisation → refused, since metrics leak volumes and shapes; a metric with a high-cardinality label → refused at registration, because a metrics explosion takes the host down; the version endpoint matching the installed package version exactly.
**Steps:** 1. Register every metric with bounded label sets and a cardinality guard. 2. Implement liveness and readiness separately with per-check reporting. 3. Implement the version endpoint from the single version source. 4. Restrict the metrics endpoint. 5. Instrument requests, jobs, providers and connectors.
**Tests:** `tests/integration/test_observability_endpoints.py` — `test_health_up_ready_false_when_database_down`, `test_unconfigured_capability_not_a_readiness_failure`, `test_metrics_requires_authorisation`, `test_high_cardinality_label_refused`, `test_version_matches_package`, `test_every_spec_metric_exported`.
**Run:** `pytest -q tests/integration/test_observability_endpoints.py` 6 passed.
**Done when:** the six tests pass and every metric in the specification is exported.
**Evidence:** a metrics scrape, a readiness response with a failing check.

---

### T-144 · Tracing and correlation across requests and jobs
`Milestone: M-6` · `Builds: N-02` · `Days: 1.0` · `Depends on: T-143` · `Snapshot: var/snapshots/T-144/` · `Build: DEFERRED — out of scope for this window`

**Goal:** one identifier follows a request or a nightly run through every layer, so "what happened here" is a search rather than an investigation.
**Context:** `spec §N-02.a`: one request id retrieves the whole story across the application log, the model log, the audit events and the traces.
**Read first:** `src/covenant_radar/core/context.py`, `observability/logging.py`, `scheduler/runner.py`.
**Contracts:** none.
**Files owned:** `src/covenant_radar/observability/tracing.py`, `src/covenant_radar/core/context.py` (job propagation), `tests/integration/test_correlation.py`
**Behaviour:** a span per request and per job step with attributes for route, principal, outcome and duration; propagation into repositories, providers and connectors; configurable sampling with errors always sampled; the same identifier on every log line, audit event, trace row and model call.
**Every case:** a background job → its own identifier propagated identically, so a nightly run is as traceable as a request; a provider call → a child span carrying the provider and model without the prompt; an error → always sampled regardless of the rate, because the sampled-away errors are the ones you need; tracing disabled → no cost beyond the context variable, and every other correlation still working; an identifier searched across all four sinks → returning the complete story, asserted by a test.
**Steps:** 1. Implement span creation and propagation. 2. Propagate into jobs. 3. Add attributes without sensitive values. 4. Implement always-sample-errors. 5. Write the four-sink correlation test.
**Tests:** `tests/integration/test_correlation.py` — `test_one_id_across_four_sinks`, `test_job_id_propagates_like_request_id`, `test_provider_span_has_no_prompt`, `test_errors_always_sampled`, `test_tracing_disabled_leaves_correlation_intact`.
**Run:** `pytest -q tests/integration/test_correlation.py` 5 passed.
**Done when:** the five tests pass and one identifier returns the whole story.
**Evidence:** a correlated search across all four sinks.

---

### T-145 · SLO definitions, alert rules and the runbook mapping
`Milestone: M-6` · `Builds: N-02` · `Days: 1.5` · `Depends on: T-144` · `Snapshot: var/snapshots/T-145/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the sixteen alerts in the specification exist, each pointing at a runbook section that exists.
**Context:** `spec §20`'s table names every signal, its threshold, who it wakes and its runbook. `spec §N-02.d`: every alert rule names a runbook section that exists, enforced by a test.
**Read first:** `spec §20`, `src/covenant_radar/observability/metrics.py`.
**Contracts:** the alert definitions `T-164`'s runbook answers.
**Files owned:** `src/covenant_radar/observability/slo.py`, `deploy/alerts/*.yml`, `docs/runbook/_index.md`, `tests/unit/test_alert_rules.py`
**Behaviour:** SLO definitions with objectives and error budgets; alert rules for every signal in `spec §20`; each rule naming its runbook section and its owner role; a burn-rate alert for the availability and latency objectives.
**Every case:** a rule naming a runbook section that does not exist → the test fails; a runbook section with no rule pointing at it → reported, because an unreachable runbook is a section nobody will find in an incident; a rule with no owner → refused; the override-rate and amber-share alerts present, since those are the two that detect the product being wrong rather than broken; an alert firing during a planned maintenance window → suppressed by the documented mechanism, not by silencing the rule.
**Steps:** 1. Define the SLOs with objectives and budgets. 2. Write a rule for every signal in `spec §20`. 3. Add owners and runbook references. 4. Implement burn-rate alerting. 5. Write the bidirectional rule-to-runbook test. 6. Document maintenance suppression.
**Tests:** `tests/unit/test_alert_rules.py` — `test_every_spec_signal_has_a_rule`, `test_every_rule_names_an_existing_runbook_section`, `test_every_runbook_section_has_a_rule`, `test_every_rule_has_an_owner`, `test_product_correctness_alerts_present`.
**Run:** `pytest -q tests/unit/test_alert_rules.py` 5 passed.
**Done when:** the five tests pass and every alert has a runbook section.
**Evidence:** the rule-to-runbook mapping.

---

### T-146 · Performance test suite and the capacity model
`Milestone: M-6` · `Builds: N-05` · `Days: 2.0` · `Depends on: T-121` · `Snapshot: var/snapshots/T-146/` · `Build: DEFERRED — out of scope for this window`

**Goal:** every row of `spec §18` measured on the reference hardware, and a capacity model the customer can size a host from.
**Context:** `spec §N-05.c`: the 10× portfolio is measured and its numbers published even where they miss. A published miss is information; an unmeasured target is a hope.
**Read first:** `spec §18`, `evaluation/reference_portfolio/generator.py`, `scheduler/pipeline.py`.
**Contracts:** `C-76` `radarctl perf`.
**Files owned:** `tests/perf/*`, `src/covenant_radar/cli.py` (the perf command), `docs/capacity-model.md`, `tests/perf/conftest.py`
**Behaviour:** a scripted load for every `spec §18` row at the reference size and at 10×; a printed table with measured, target and pass or miss; a stored run; a capacity model relating portfolio size to batch duration, database growth and memory.
**Every case:** a miss → printed with both numbers and the run still exiting zero without `--check`, so measurement is not gated on success; the 10× run → executed and published even when it misses; a measurement varying more than the configured tolerance between runs → flagged as unstable rather than reported as a number, because an unstable measurement is not a measurement; the reference hardware differing from the specification → recorded in the run so numbers are comparable; the capacity model derived from measurements at three sizes, not extrapolated from one.
**Steps:** 1. Build the scripted loads for each row. 2. Generate the 10× portfolio into a separate database. 3. Implement the printed table, the stored run and `--check`. 4. Implement stability detection across repeats. 5. Measure at three sizes and derive the capacity model.
**Tests:** `tests/perf/test_all_rows.py` — `test_every_spec_18_row_measured`, `test_miss_printed_and_exit_zero_without_check`, `test_unstable_measurement_flagged`, `test_hardware_recorded_in_run`; `tests/perf/test_capacity_model.py` — `test_model_derived_from_three_sizes`.
**Run:** `radarctl perf` prints every row · `pytest -q tests/perf` all passed.
**Done when:** every row is measured at both sizes and the capacity model is derived from three measured points.
**Evidence:** the performance table, the capacity model.

---

### T-147 · Performance remediation to the specification's targets
`Milestone: M-6` · `Builds: N-05` · `Days: 1.5` · `Depends on: T-146` · `Snapshot: var/snapshots/T-147/` · `Build: DEFERRED — out of scope for this window`

**Goal:** close the gap between measured and target, by changing what the measurement shows is slow rather than what feels slow.
**Context:** `spec §18`. Optimisation without a measurement is superstition; this task starts from `T-146`'s table and ends when the rows pass or a miss is documented with its reason and its cost to fix.
**Read first:** `tests/perf/` results, the query plans, `db/repositories/`.
**Contracts:** unchanged — this task alters no contract.
**Files owned:** the specific modules each finding names, `docs/adr/0004-performance-decisions.md`, `src/covenant_radar/db/migrations/versions/*` (index additions only)
**Behaviour:** each missing row profiled, its dominant cost identified, and the fix applied — index, query shape, caching of an immutable value, eager loading, or batch size — with the before and after recorded.
**Every case:** a fix that improves a number by weakening a guarantee, such as caching a scoped read across principals → refused, and the alternative documented; an index added → its plan verified as used, since an unused index is cost without benefit; a remaining miss → documented with its cause, its cost to fix and a recommendation, never quietly re-targeted; a fix improving one row and worsening another → the whole table re-run, because performance work without a full re-run is a trade nobody measured; every fix covered by an existing behavioural test so correctness is preserved.
**Steps:** 1. Profile each missing row and record the dominant cost. 2. Apply the narrowest fix. 3. Verify index usage by plan. 4. Re-run the whole table after each fix. 5. Record the decisions and any remaining miss in the ADR.
**Tests:** the existing behavioural suites must remain green; `tests/perf/test_all_rows.py` re-run after every change.
**Run:** `radarctl perf --check` exit 0, or exit non-zero with every remaining miss documented in the ADR · `python -m radarctl gate` green.
**Done when:** every row passes, or each remaining miss is documented with its cause, cost and recommendation.
**Evidence:** before-and-after tables, the ADR.

---

### T-148 · Backup, restore and the rehearsal tooling
`Milestone: M-6` · `Builds: N-06` · `Days: 2.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-148/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a restore that has actually been performed and timed, because a backup nobody has restored is a hope with a schedule.
**Context:** `spec §N-06.a`: a restore into an empty host reproduces the system and meets the stated recovery objectives, rehearsed and timed.
**Read first:** `plan.md §5`, `src/covenant_radar/documents/store.py`, `config/settings.py`.
**Contracts:** `C-78` `radarctl backup | restore`.
**Files owned:** `deploy/backup/*`, `src/covenant_radar/services/backup.py`, `src/covenant_radar/cli.py` (backup and restore), `docs/admin-guide/backup-restore.md`, `tests/integration/test_backup_restore.py`
**Behaviour:** a consistent backup of the database, the document store and the configuration, encrypted, with a manifest and a checksum; a restore that verifies the manifest before writing anything; a rehearsal command that restores into a scratch location and reports the elapsed time against the objective.
**Every case:** a backup taken while the nightly batch is running → consistent, using a database snapshot rather than a file copy, and a test proves the restored database is not mid-transaction; a restore into a non-empty target → refused without an explicit flag; a corrupt archive → detected by the manifest before anything is written; a restore of a backup from an older schema → migrations applied and the version recorded, or refused if the gap exceeds the supported range; a rehearsal → non-destructive by construction, with the elapsed time recorded against the objective.
**Steps:** 1. Implement the consistent backup with encryption and a manifest. 2. Implement restore with pre-write verification and the non-empty guard. 3. Implement cross-version restore with migration. 4. Implement the non-destructive rehearsal with timing. 5. Write the procedure documentation.
**Tests:** `tests/integration/test_backup_restore.py` — `test_backup_during_batch_is_consistent`, `test_corrupt_archive_detected_before_write`, `test_restore_into_non_empty_refused`, `test_older_schema_restored_with_migration`, `test_rehearsal_non_destructive_and_timed`, `test_restore_reproduces_content_exactly`.
**Run:** `pytest -q tests/integration/test_backup_restore.py` 6 passed · a rehearsal completing inside the recovery objective.
**Done when:** the six tests pass and a timed rehearsal meets the objective.
**Evidence:** the rehearsal timing record.

---

### [x] `T-149` · Graceful shutdown, startup self-checks, pool resilience
`Milestone: M-6` · `Builds: N-06` · `Days: 1.0` · `Depends on: T-143` · `Snapshot: var/snapshots/T-149/` · `Build: complete (implemented on demand, outside the numbered build sequence)`

**Goal:** the process starts only when it can work, stops without losing work, and survives a database blip without a restart.
**Context:** `spec §N-06.b`: a hard kill during the overnight batch leaves no partial state and the retry completes correctly.
**Read first:** `src/covenant_radar/asgi.py`, `scheduler/runner.py`, `db/session.py`.
**Contracts:** `C-70`.
**Files owned:** `src/covenant_radar/lifecycle.py`, `src/covenant_radar/db/session.py` (resilience), `src/covenant_radar/scheduler/runner.py` (shutdown), `tests/integration/test_lifecycle.py`
**Behaviour:** startup self-checks for configuration, migrations at head, database, document store and scheduler, refusing to start on a failure with the check named; graceful shutdown draining in-flight requests and finishing or cleanly abandoning job steps; connection pooling with pre-ping, retry and a circuit breaker.
**Every case:** migrations behind head → refuse to start naming the pending revisions, because running against an older schema corrupts quietly; a database blip → the circuit opens, requests fail fast with a maintenance response, and the circuit closes on recovery without a restart; a shutdown signal during a job step → the step finishes or is cleanly abandoned per its policy, with the run ledger recording which; a hard kill → the next start finds no partial state, proven by a test; a self-check failing at startup → the process exits non-zero with the check named, never starts degraded silently.
**Steps:** 1. Implement the startup self-checks. 2. Implement graceful shutdown for the web and scheduler paths. 3. Implement pool pre-ping, retry and the circuit breaker. 4. Implement the maintenance response. 5. Test the hard-kill recovery.
**Tests:** `tests/integration/test_lifecycle.py` — `test_pending_migrations_refuse_start`, `test_database_blip_opens_and_closes_circuit`, `test_shutdown_records_step_disposition`, `test_hard_kill_leaves_no_partial_state`, `test_failed_self_check_exits_non_zero_naming_it`.
**Run:** `pytest -q tests/integration/test_lifecycle.py` 5 passed.
**Done when:** the five tests pass and a hard kill leaves nothing partial.
**Evidence:** the hard-kill recovery transcript.

---

### [x] `T-150` · Data-integrity checks: audit chain and referential
`Milestone: M-6` · `Builds: N-06` · `Days: 1.0` · `Depends on: T-066` · `Snapshot: var/snapshots/T-150/` · `Build: complete (implemented on demand, outside the numbered build sequence)`

**Goal:** a scheduled check that would notice if something silently went wrong.
**Context:** `spec §N-06.c`: the integrity check detects a deliberately corrupted audit chain. It also checks referential integrity, orphaned records and threshold-snapshot references, since those are the failures that make a reconstruction wrong rather than absent.
**Read first:** `src/covenant_radar/audit/chain.py`, `plan.md §5`.
**Contracts:** the check's alert; `C-23`'s readiness reporting.
**Files owned:** `src/covenant_radar/services/integrity.py`, `src/covenant_radar/scheduler/jobs.py` (the integrity job), `tests/integration/test_integrity_checks.py`
**Behaviour:** a scheduled verification of the audit chain, referential integrity across every foreign key, orphan detection, snapshot-reference validity and document-store consistency, reporting per check and alerting on any failure.
**Every case:** a corrupted chain row → detected, the sequence named, an alert raised at the highest severity, and the check recorded; a missing document file whose record exists → detected and reported, since the record promises the file; a record referencing a purged snapshot → detected, because a forecast whose threshold snapshot is gone cannot be explained; a check running against a large database → incremental with a watermark so it never becomes too slow to run; a clean run → recorded, because absence of a report is indistinguishable from a check that did not run.
**Steps:** 1. Implement chain verification incrementally. 2. Implement referential and orphan checks. 3. Implement snapshot-reference and document-store checks. 4. Implement per-check reporting and alerting. 5. Schedule and record every run.
**Tests:** `tests/integration/test_integrity_checks.py` — `test_corrupted_chain_detected_and_alerted`, `test_missing_document_file_reported`, `test_purged_snapshot_reference_detected`, `test_incremental_with_watermark`, `test_clean_run_recorded`.
**Run:** `pytest -q tests/integration/test_integrity_checks.py` 5 passed.
**Done when:** the five tests pass and a deliberately corrupted chain is detected.
**Evidence:** the corruption-detection report.

---

### T-151 · Linux installer, service unit and post-install verification
`Milestone: M-6` · `Builds: N-08` · `Days: 2.0` · `Depends on: T-149` · `Snapshot: var/snapshots/T-151/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a customer's administrator installs the product on a fresh host and reaches a working system from the guide alone.
**Context:** `spec §N-08.a`: a clean install performed by someone who did not write the installer. That constraint is the test; an installer written and run by its author proves nothing.
**Read first:** `spec §25.1`, `src/covenant_radar/lifecycle.py`, `config/production.example.toml`.
**Contracts:** `C-70`, `C-71`, `C-72`, `C-73`.
**Files owned:** `deploy/linux/install.sh`, `covenant-radar.service`, `nginx.conf.example`, `deploy/linux/verify.sh`, `docs/admin-guide/install-linux.md`, `tests/integration/test_install_linux.py`
**Behaviour:** provisioning the runtime and virtual environment, creating or connecting the database, running migrations, seeding reference data, registering the systemd unit with a dedicated unprivileged user, creating directories with correct permissions, configuring TLS, creating the first administrator with a forced password change, and running a post-install verification reporting each check as pass, fail or not-configured.
**Every case:** a prerequisite missing → the installer stops before changing anything, naming what to install; an existing installation → the installer refuses and directs to the upgrade command, since an installer that overwrites is a data-loss event; a database with existing data → connected, not initialised; TLS unconfigured → the installer completes with a prominent warning and the service bound to loopback only, never exposed unencrypted; the verification failing a check → reported with its remediation, and the service left stopped rather than running broken; a first administrator password → generated and shown once, never written to a file.
**Steps:** 1. Write the prerequisite checks and the fail-before-change guard. 2. Provision runtime, virtual environment and directories with permissions. 3. Connect or create the database and run migrations and seed. 4. Register the service under an unprivileged user. 5. Configure TLS with the loopback fallback. 6. Write the verification script and the guide.
**Tests:** `tests/integration/test_install_linux.py` — `test_missing_prerequisite_stops_before_change`, `test_existing_installation_directs_to_upgrade`, `test_existing_database_connected_not_initialised`, `test_no_tls_binds_loopback_with_warning`, `test_verification_failure_leaves_service_stopped`, `test_admin_password_shown_once_never_written`.
**Run:** `pytest -q tests/integration/test_install_linux.py` 6 passed · a clean container install reaching a working system.
**Done when:** the six tests pass and a clean install works from the guide alone.
**Evidence:** an install transcript, the verification output.

---

### T-152 · Windows installer, service registration and verification
`Milestone: M-6` · `Builds: N-08` · `Days: 2.0` · `Depends on: T-151` · `Snapshot: var/snapshots/T-152/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the same install on Windows Server, because that is what many Indian banks actually run.
**Context:** `spec §11.1` names Windows Server 2022+ as a supported target. The Windows path has its own realities — service accounts, ACLs, certificate stores, execution policy — and each is handled explicitly rather than assumed similar to Linux.
**Read first:** `deploy/linux/install.sh`, `docs/admin-guide/install-linux.md`.
**Contracts:** the same as `T-151`.
**Files owned:** `deploy/windows/install.ps1`, `service.ps1`, `verify.ps1`, `docs/admin-guide/install-windows.md`, `tests/integration/test_install_windows.py`
**Behaviour:** the same sequence as the Linux installer, using a Windows service under a dedicated account, ACLs on the data and log directories, the certificate store for TLS, and the same post-install verification.
**Every case:** an execution policy blocking the script → detected and the required policy named, rather than failing obscurely; a service account without log-on-as-a-service → detected and the grant named; directory ACLs → set explicitly so the service account can write and others cannot, asserted by the verification; a certificate in the store → referenced by thumbprint rather than exported to a file; a path with spaces → handled throughout, tested with a deliberately spaced installation path.
**Steps:** 1. Port the sequence with Windows-specific provisioning. 2. Create the service account and grant the right. 3. Register the service with recovery actions. 4. Set ACLs and verify them. 5. Wire TLS from the certificate store. 6. Write the verification and the guide.
**Tests:** `tests/integration/test_install_windows.py` — `test_execution_policy_named_when_blocking`, `test_missing_logon_right_named`, `test_acls_set_and_verified`, `test_certificate_referenced_by_thumbprint`, `test_spaced_install_path_handled`, `test_verification_matches_linux_checks`.
**Run:** `pytest -q tests/integration/test_install_windows.py` 6 passed · a clean Windows install reaching a working system.
**Done when:** the six tests pass and the verification reports the same checks as the Linux path.
**Evidence:** an install transcript, the ACL verification.

---

### T-153 · Upgrade command with automatic rollback
`Milestone: M-6` · `Builds: N-08` · `Days: 2.0` · `Depends on: T-152, T-148` · `Snapshot: var/snapshots/T-153/` · `Build: DEFERRED — out of scope for this window`

**Goal:** upgrading is one command that leaves a working system whichever way it goes.
**Context:** `spec §N-08.c`: a deliberately failing upgrade rolls back automatically and the system is left working on the prior version. An upgrade that can leave a customer with neither version is the failure that ends a deployment relationship.
**Read first:** `deploy/linux/install.sh`, `deploy/windows/install.ps1`, `services/backup.py`.
**Contracts:** `C-71`, `C-78`.
**Files owned:** `deploy/upgrade/upgrade.sh`, `upgrade.ps1`, `rollback.md`, `src/covenant_radar/services/upgrade.py`, `docs/admin-guide/upgrade.md`, `tests/integration/test_upgrade.py`
**Behaviour:** pre-flight compatibility check, backup, stop, install, migrate, start, verify — and on any failure, automatic restore and restart of the prior version with a report; a documented manual rollback for when the automatic path cannot run.
**Every case:** a failure at any step → automatic rollback, the prior version working, and a report naming the failing step; a migration that is not reversible → the rollback restores the backup rather than downgrading, and the procedure says so explicitly; a version gap larger than supported → refused before the backup, naming the supported path; the verification failing after a successful migration → rollback, since a system that migrated but does not work is worse than one that did not migrate; the upgrade run twice → the second recognising the current version and exiting cleanly.
**Steps:** 1. Implement the compatibility pre-flight and the version-gap guard. 2. Implement the sequence with a checkpoint after each step. 3. Implement automatic rollback from the checkpoint. 4. Handle irreversible migrations by restore. 5. Write the manual rollback procedure and the guide.
**Tests:** `tests/integration/test_upgrade.py` — `test_failure_at_each_step_rolls_back` (parameterised), `test_irreversible_migration_restores_backup`, `test_version_gap_refused_before_backup`, `test_post_migration_verification_failure_rolls_back`, `test_rerun_exits_cleanly`, `test_data_preserved_across_upgrade`.
**Run:** `pytest -q tests/integration/test_upgrade.py` all passed · an upgrade from the prior tag succeeding, and a deliberately failed one rolling back.
**Done when:** every step's failure rolls back to a working prior version.
**Evidence:** the parameterised rollback transcript.

---

### T-154 · Container image and compose alternative
`Milestone: M-6` · `Builds: N-08` · `Days: 1.0` · `Depends on: T-151` · `Snapshot: var/snapshots/T-154/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the customers who prefer containers get one, producing an identical application.
**Context:** `spec §25.1`: containers are supported, never required. The image must be the same code with the same configuration surface, so support is not answering two different products.
**Read first:** `deploy/linux/install.sh`, `pyproject.toml`.
**Contracts:** unchanged.
**Files owned:** `deploy/container/Containerfile`, `compose.yaml`, `entrypoint.sh`, `docs/admin-guide/install-container.md`, `tests/integration/test_container.py`
**Behaviour:** a multi-stage build producing a minimal image running as a non-root user with a read-only root filesystem and explicit volumes; a compose file adding PostgreSQL for evaluation; the same configuration variables as the native install.
**Every case:** the image run as root → refused by the entrypoint, since a container running as root removes the isolation it was chosen for; a missing required variable → the container exits with the variable named rather than restarting in a loop; the same configuration → producing the same behaviour as a native install, asserted by running one test suite against both; a volume not mounted → the container refuses to start naming it, rather than writing to an ephemeral layer; the image scanned in CI with findings at or above the configured severity failing the build.
**Steps:** 1. Write the multi-stage build with a non-root user. 2. Write the entrypoint with the variable and volume checks. 3. Write the compose file for evaluation. 4. Add image scanning to CI. 5. Assert behavioural equivalence with the native install.
**Tests:** `tests/integration/test_container.py` — `test_refuses_to_run_as_root`, `test_missing_variable_exits_naming_it`, `test_missing_volume_refuses_start`, `test_behaviour_matches_native_install`, `test_image_scan_clean`.
**Run:** `pytest -q tests/integration/test_container.py` 5 passed.
**Done when:** the five tests pass and the container behaves identically to the native install.
**Evidence:** the equivalence-test output, the scan report.

---

### T-155 · Reproducible build, lock verification and SBOM
`Milestone: M-6` · `Builds: N-08` · `Days: 1.0` · `Depends on: T-001` · `Snapshot: var/snapshots/T-155/` · `Build: DEFERRED — out of scope for this window`

**Goal:** any released version can be rebuilt from its tag with an identical dependency set, and the customer's security team gets the bill of materials they will ask for.
**Context:** `spec §N-08.d`. A customer under the IT-Outsourcing Direction needs to know exactly what is running, and "we can rebuild it" is a different claim from "we can rebuild it identically".
**Read first:** `pyproject.toml`, `requirements.lock`, `.github/workflows/release.yml`.
**Contracts:** none.
**Files owned:** `.github/workflows/release.yml` (build and SBOM), `scripts/verify_build.py`, `docs/admin-guide/software-bill-of-materials.md`, `tests/unit/test_build_reproducibility.py`
**Behaviour:** a build from a tag producing a deterministic artefact with pinned dependencies and normalised timestamps; a CycloneDX bill of materials with licences; a verification command comparing an installed deployment against its manifest.
**Every case:** two builds from the same tag → identical artefact hashes, asserted in CI; a lock file that does not resolve reproducibly → the build fails; a dependency whose licence is outside the allowed list → the build fails naming it and the licence; an installed deployment whose files differ from the manifest → reported by the verification command, which is how a support engineer detects a hand-edited production system; the bill of materials published with the release.
**Steps:** 1. Normalise the build for determinism. 2. Verify the lock resolves reproducibly. 3. Generate the bill of materials with licences and add the licence-policy check. 4. Write the deployment verification command. 5. Assert build equality in CI.
**Tests:** `tests/unit/test_build_reproducibility.py` — `test_two_builds_identical`, `test_non_reproducible_lock_fails`, `test_disallowed_licence_fails_naming_it`, `test_deployment_verification_detects_modification`.
**Run:** `pytest -q tests/unit/test_build_reproducibility.py` 4 passed · two CI builds producing identical hashes.
**Done when:** two builds from one tag are identical and the bill of materials generates.
**Evidence:** the two build hashes, the bill of materials.

---

### T-156 · Property-based tests for domain invariants
`Milestone: M-6` · `Builds: N-09` · `Days: 1.5` · `Depends on: T-063` · `Snapshot: var/snapshots/T-156/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the domain's invariants tested against generated inputs, not only the cases someone thought of.
**Context:** `spec §N-09`. Several suites already carry property tests; this task completes the set, consolidates the generators and makes the invariants explicit as a list a reviewer can read.
**Read first:** every module under `src/covenant_radar/domain/`, the existing `tests/property/`.
**Contracts:** the domain contracts `C-30`–`C-41`.
**Files owned:** `tests/property/*`, `tests/property/strategies.py`, `docs/domain-invariants.md`
**Behaviour:** generators for statements, covenants, evidence series and facilities that produce realistic and adversarial values; invariants asserted across ratios, headroom, persistence, materiality, decay, projection, probability, confidence, attribution, banding and simulation.
**Every case:** a falsifying example → the test fails with the minimal case, and the example is added to the regression suite so it is never re-found; a generator producing an impossible combination, such as a negative sanctioned limit → excluded by the strategy, since testing impossible states wastes the budget; every invariant in the document having a test and every test naming its invariant, asserted bidirectionally; the suite bounded in time so it stays in the commit gate rather than drifting into a nightly.
**Steps:** 1. Build the strategies with realistic and adversarial ranges. 2. Write the invariants document. 3. Implement one test per invariant. 4. Wire falsifying examples into a regression corpus. 5. Bound the runtime for the gate.
**Tests:** `tests/property/test_invariant_coverage.py` — `test_every_documented_invariant_has_a_test`, `test_every_test_names_an_invariant`; plus the invariant tests themselves across the eleven areas.
**Run:** `pytest -q tests/property` all passed within the time bound.
**Done when:** every documented invariant has a test and the suite runs inside the gate's budget.
**Evidence:** the invariants document with test references.

---

### T-157 · Migration upgrade tests from the prior release
`Milestone: M-6` · `Builds: N-09` · `Days: 1.0` · `Depends on: T-010` · `Snapshot: var/snapshots/T-157/` · `Build: DEFERRED — out of scope for this window`

**Goal:** an upgrade over real-shaped data is tested before a customer performs it.
**Context:** `spec §N-08.b`. A migration that passes on an empty database and fails on a populated one is the most common upgrade failure there is.
**Read first:** `src/covenant_radar/db/migrations/versions/`, `evaluation/reference_portfolio/`.
**Contracts:** `C-71`.
**Files owned:** `tests/migration/*`, `tests/migration/fixtures/*`
**Behaviour:** a representative dataset seeded at the prior release's schema, upgraded to head, and verified for row counts, referential integrity, audit-chain validity and a sample of business values; downgrade tested where declared reversible.
**Every case:** a data-moving migration → verified for both completeness and correctness, not only that it ran; a migration taking longer than the configured budget on the representative dataset → flagged, because an upgrade window is a real constraint for a customer; an irreversible migration → its downgrade refusing explicitly rather than appearing to succeed; the audit chain after migration → still valid, since a migration that renumbers or rewrites audit rows breaks the product's central guarantee; every released schema version having an upgrade test.
**Steps:** 1. Build the representative dataset at the prior schema. 2. Implement the upgrade test with the four verifications. 3. Add the duration budget. 4. Test declared downgrades and irreversible refusals. 5. Assert coverage across released versions.
**Tests:** `tests/migration/test_upgrade_from_prior.py` — `test_upgrade_preserves_counts_and_integrity`, `test_audit_chain_valid_after_migration`, `test_data_moving_migration_correct`, `test_duration_within_budget`, `test_irreversible_downgrade_refuses`, `test_every_released_version_covered`.
**Run:** `pytest -q tests/migration` all passed.
**Done when:** every released version upgrades cleanly over representative data with the chain intact.
**Evidence:** the upgrade transcript with timings.

---

### T-158 · Authorization test matrix: every role against every endpoint
`Milestone: M-6` · `Builds: N-09` · `Days: 2.0` · `Depends on: T-136` · `Snapshot: var/snapshots/T-158/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the security claim proven exhaustively — every role tried against every endpoint, with the result compared to the specification's matrix.
**Context:** `spec §N-04.b` and `N-09.b`. Two rows must hold in every configuration: nobody may confirm a covenant that failed verification, and nobody may take a credit decision in the tool.
**Read first:** `spec §16.1` (the matrix), `src/covenant_radar/security/permissions.py`, every router.
**Contracts:** every route contract.
**Files owned:** `tests/security/test_authorization_matrix.py`, `tests/security/matrix.yaml`, `docs/compliance/authorization-matrix.md`
**Behaviour:** the specification's matrix encoded as data; a generated test per role per endpoint asserting allowed or refused; a scope test per role per endpoint; a report published as a compliance artefact.
**Every case:** an endpoint absent from the matrix → the test fails, so a new endpoint cannot ship unclassified; a role permitted something the specification forbids → failure; the two never-permitted operations → asserted for every role and every authentication method, including API keys; a refusal returning the wrong status, such as `403` where scope requires `404` → failure, because the distinction is a security property; the matrix document generated from the same data as the tests, so it cannot drift.
**Steps:** 1. Encode the matrix as data. 2. Generate the per-role per-endpoint tests. 3. Add scope assertions. 4. Assert endpoint coverage bidirectionally. 5. Generate the compliance document from the same source.
**Tests:** `tests/security/test_authorization_matrix.py` — `test_every_endpoint_classified`, `test_every_role_endpoint_pair_matches_matrix`, `test_scope_refusals_use_404`, `test_never_permitted_operations_refused_for_all`, `test_api_keys_follow_the_same_matrix`.
**Run:** `pytest -q tests/security/test_authorization_matrix.py` all passed with the full matrix generated.
**Done when:** every pair is asserted and the generated document matches the specification.
**Evidence:** the generated matrix document.

---

### T-159 · End-to-end suite across themes
`Milestone: M-6` · `Builds: N-09` · `Days: 2.0` · `Depends on: T-159 is withdrawn from the language axis; re-scope to T-083 when restored` · `Snapshot: var/snapshots/T-159/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the flows a user actually performs, exercised in a real browser, in both themes (the language axis is withdrawn with `T-141`).
**Context:** `spec §F-01` through `F-08`. Unit tests prove the parts; only the end-to-end suite proves that a credit officer can get from the queue to a memo.
**Read first:** `spec §9` (the eight flows), the existing `tests/e2e/`.
**Contracts:** the screen contracts.
**Files owned:** `tests/e2e/*`, `tests/e2e/pages/*`, `tests/e2e/conftest.py`
**Behaviour:** one test per flow, driven through page objects, run in both themes and both languages, with screenshots captured on every step and on failure; deterministic against the reference portfolio.
**Every case:** a flow failing → a screenshot, the page markup and the correlated server logs captured, so a failure is diagnosable without reproducing it; a flow passing in English and failing in Hindi → reported as the layout defect it is, not retried; a test depending on wall-clock time → refused, since a suite that fails at midnight is a suite people disable; the suite runnable against a fresh install as well as a seeded one; every flow in `spec §9` covered, asserted bidirectionally.
**Steps:** 1. Build page objects for every screen. 2. Write one test per flow. 3. Parameterise across themes and languages. 4. Capture diagnostics on failure. 5. Assert flow coverage. 6. Remove every wall-clock dependency.
**Tests:** `tests/e2e/test_flows.py` — one test per flow, parameterised; `tests/e2e/test_flow_coverage.py` — `test_every_spec_flow_covered`.
**Run:** `pytest -q tests/e2e` all passed in both themes and both languages.
**Done when:** all eight flows pass in every combination and diagnostics are captured on failure.
**Evidence:** the run report with screenshots.

---

### T-160 · Mutation testing and coverage gates
`Milestone: M-6` · `Builds: N-09` · `Days: 1.5` · `Depends on: T-156` · `Snapshot: var/snapshots/T-160/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the tests are shown to actually test something, and the coverage thresholds the specification names are enforced rather than reported.
**Context:** `spec §N-09.a` and `N-09.d`: coverage thresholds enforced in CI and a mutation score at or above its recorded floor. Coverage without mutation testing measures execution, not verification.
**Read first:** `pyproject.toml` (the coverage configuration), `tests/unit/`, `src/covenant_radar/domain/`.
**Contracts:** none.
**Files owned:** `pyproject.toml` (thresholds), `mutmut_config.py`, `.github/workflows/ci.yml` (the mutation job), `docs/testing.md`, `tests/unit/test_coverage_gates.py`
**Behaviour:** coverage thresholds raised to the specification's figures and enforced; mutation testing over the domain on a schedule and before release, with a recorded floor; surviving mutants triaged into killed, equivalent or accepted with a reason.
**Every case:** coverage below its threshold → the build fails naming the module; a surviving mutant in the ratio, engine, ledger or forecast code → treated as a missing test rather than an accepted mutant, because those four are where a wrong number would be most costly; an equivalent mutant → recorded with its justification so it is not re-triaged every run; the mutation score below its floor → the release is blocked; the score floor raised only with a recorded justification.
**Steps:** 1. Raise the coverage thresholds and enforce them. 2. Configure mutation testing scoped to the domain. 3. Establish the floor and the triage record. 4. Kill the survivors in the four critical modules. 5. Add the jobs to CI and the release gate.
**Tests:** `tests/unit/test_coverage_gates.py` — `test_thresholds_match_specification`, `test_mutation_floor_recorded`, `test_survivors_triaged_with_reasons`, `test_critical_modules_have_no_accepted_survivors`.
**Run:** `pytest -q tests/unit/test_coverage_gates.py` 4 passed · coverage at or above the thresholds · the mutation score at or above its floor.
**Done when:** coverage and mutation thresholds are enforced and the four critical modules have no accepted survivors.
**Evidence:** the coverage report, the mutation report with triage.

---

### T-161 · Administrator guide
`Milestone: M-6` · `Builds: N-10` · `Days: 1.5` · `Depends on: T-153` · `Snapshot: var/snapshots/T-161/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a new administrator installs, configures and operates the system from this document alone.
**Context:** `spec §N-10.a`: verified by observation, not by the author's judgement. A guide that only works for someone who already knows the product is not a guide.
**Read first:** every `deploy/` script, `src/covenant_radar/config/settings.py`, `docs/runbook/`.
**Contracts:** the commands and configuration surface.
**Files owned:** `docs/admin-guide/*`
**Behaviour:** install for both platforms and containers; every configuration option with its effect and default; user, role and scope administration; connector and feed configuration with a worked example; notification setup; threshold governance; job operation; backup, restore and upgrade; troubleshooting by symptom.
**Every case:** every configuration option documented, asserted by a test comparing the guide against the settings model, so an undocumented option fails the build; every command documented with a worked example; every troubleshooting entry naming a symptom an administrator would actually observe rather than an internal error class; a step requiring a decision → stating the trade-off rather than only the mechanics; the guide reviewed by someone who did not write it.
**Steps:** 1. Write the install, configure, operate, maintain and troubleshoot sections. 2. Include worked examples for the connector and notification setup. 3. Write troubleshooting by observable symptom. 4. Add the settings-coverage test. 5. Have it reviewed by a non-author.
**Tests:** `tests/unit/test_documentation.py` — `test_every_setting_documented`, `test_every_command_documented`, `test_troubleshooting_entries_are_symptom_led`.
**Run:** `pytest -q tests/unit/test_documentation.py -k admin` passed · a non-author completes an install from the guide.
**Done when:** every setting and command is documented and a non-author installs from the guide alone.
**Evidence:** the observed install, the review note.

---

### T-162 · User guides per role
`Milestone: M-6` · `Builds: N-10` · `Days: 1.5` · `Depends on: T-159` · `Snapshot: var/snapshots/T-162/` · `Build: DEFERRED — out of scope for this window`

**Goal:** each role can learn their own job from a short document written for them, not a manual written for everyone.
**Context:** `spec §7`'s roles and `spec §9`'s flows. A relationship manager and an auditor share almost no tasks, and one guide for both serves neither.
**Read first:** `spec §7`, `spec §9`, the screens.
**Contracts:** none.
**Files owned:** `docs/user-guide/*`
**Behaviour:** one guide per role covering that role's flows with screenshots, the vocabulary the product uses, what each number means, what the confidence floor means when a probability is absent, how to disagree, and what the product will never do.
**Every case:** every flow in `spec §9` appearing in at least one role's guide, asserted by a test; every guide stating the advisory-only posture, because a user who believes the tool decides will use it wrongly; the explanation of a suppressed probability appearing in every guide whose role sees one, since it is the most likely question; screenshots regenerated with the release so they never show a stale interface; the vocabulary section defining every domain term the interface uses.
**Steps:** 1. Write one guide per role around that role's flows. 2. Include the vocabulary and the number meanings. 3. State the advisory posture and the suppression rule in each. 4. Generate screenshots from the end-to-end suite so they stay current. 5. Assert flow coverage.
**Tests:** `tests/unit/test_documentation.py` — `test_every_flow_in_a_role_guide`, `test_every_guide_states_advisory_posture`, `test_screenshots_generated_from_suite`.
**Run:** `pytest -q tests/unit/test_documentation.py -k user` passed.
**Done when:** every flow is covered and every guide states the posture.
**Evidence:** the guides with current screenshots.

---

### T-163 · API reference and worked examples
`Milestone: M-6` · `Builds: N-10` · `Days: 0.5` · `Depends on: T-136` · `Snapshot: var/snapshots/T-163/` · `Build: DEFERRED — out of scope for this window`

**Goal:** an integrator succeeds from the reference without asking a question.
**Context:** `spec §N-10.c`: the reference matches the implementation, verified by the contract tests. It is generated from the same document those tests check.
**Read first:** `src/covenant_radar/api/openapi.py`, `docs/api/`.
**Contracts:** `C-21`, `C-22`.
**Files owned:** `docs/api/*`, `scripts/build_api_docs.py`
**Behaviour:** a reference generated from the OpenAPI document with authentication, scoping, pagination, filtering, error handling, rate limits, webhooks and the deprecation policy, plus a worked end-to-end integration example.
**Every case:** an endpoint without an example → the build fails, because an endpoint nobody documented is an endpoint nobody can use; the reference regenerating on every release so it cannot drift; the error envelope documented once with every code; the webhook verification procedure including a runnable snippet; the deprecation policy stating the notice period and the overlap guarantee.
**Steps:** 1. Generate the reference from the document. 2. Add the narrative sections. 3. Write the worked integration example. 4. Assert example coverage. 5. Wire regeneration into the release workflow.
**Tests:** `tests/unit/test_documentation.py` — `test_every_endpoint_has_an_example`, `test_reference_regenerates_from_openapi`.
**Run:** `python scripts/build_api_docs.py` exit 0 · `pytest -q tests/unit/test_documentation.py -k api` passed.
**Done when:** every endpoint has an example and the reference regenerates cleanly.
**Evidence:** the generated reference.

---

### T-164 · Operations runbook, one section per alert
`Milestone: M-6` · `Builds: N-10` · `Days: 1.0` · `Depends on: T-145` · `Snapshot: var/snapshots/T-164/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the person woken at 3 a.m. finds a page that tells them what to do.
**Context:** `spec §N-10.b`: every alert rule has a runbook section. `spec §2.1`: a cyber incident starts a six-hour reporting clock, and the security sections carry that clock in their first line.
**Read first:** `deploy/alerts/`, `src/covenant_radar/observability/slo.py`, `spec §20`.
**Contracts:** the alert definitions.
**Files owned:** `docs/runbook/*`
**Behaviour:** one section per alert with what it means, what to check first, the likely causes in order, the remediation, the escalation path and how to confirm recovery; plus incident-response procedures including the CERT-In clock and a diagnostic-bundle procedure.
**Every case:** every rule having a section and every section having a rule, asserted bidirectionally; each section naming a first check that is a single command, because a runbook that opens with analysis is a runbook nobody follows under pressure; the security sections opening with the reporting clock; every remediation tested at least once against a simulated condition, so the runbook is known to work; the escalation path naming roles, not individuals.
**Steps:** 1. Write one section per alert in the fixed structure. 2. Open every section with a single first command. 3. Write the incident-response and diagnostic-bundle procedures with the clock. 4. Simulate each condition once and correct the section. 5. Assert the bidirectional mapping.
**Tests:** `tests/unit/test_documentation.py` — `test_every_alert_has_a_section`, `test_every_section_has_an_alert`, `test_every_section_opens_with_a_command`, `test_security_sections_carry_the_clock`.
**Run:** `pytest -q tests/unit/test_documentation.py -k runbook` passed.
**Done when:** every alert has a tested section and every section opens with a command.
**Evidence:** the runbook, the simulation notes.

---

### T-165 · Architecture decision records and model cards
`Milestone: M-6` · `Builds: N-10` · `Days: 1.0` · `Depends on: T-107` · `Snapshot: var/snapshots/T-165/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the decisions behind the code are readable, so the next engineer changes them deliberately rather than by accident.
**Context:** `plan.md §11.5`. Several records were written as their decisions were made; this task completes the set and writes the model cards the FREE-AI expectation and `spec §N-12` both call for.
**Read first:** `docs/adr/`, `src/covenant_radar/ai/registry.py`, `plan.md §14.3`.
**Contracts:** none.
**Files owned:** `docs/adr/*`, `docs/model-cards/*`, `docs/architecture.md`
**Behaviour:** one record per significant decision with context, options, choice, consequences and what would change it; one model card per model-using component with purpose, inputs, outputs, limitations, evaluation results, owner and the human review it requires; an architecture overview tying them together.
**Every case:** a decision in `plan.md §14.3` without a record → the test fails; a model-using component without a card → the build fails, since an undocumented model in a regulated product is a finding; a card omitting its limitations → refused, because the limitations are the part a reviewer needs; a superseded record → marked superseded with a link forward, never deleted; the architecture overview linking every layer to its records.
**Steps:** 1. Write the outstanding records from the authored decisions. 2. Write a card per component including limitations and evaluation results. 3. Write the architecture overview. 4. Mark superseded records. 5. Assert coverage in both directions.
**Tests:** `tests/unit/test_documentation.py` — `test_every_authored_decision_has_a_record`, `test_every_model_component_has_a_card`, `test_every_card_states_limitations`, `test_superseded_records_link_forward`.
**Run:** `pytest -q tests/unit/test_documentation.py -k adr` passed.
**Done when:** every decision and every component is documented.
**Evidence:** the record and card index.

---

### T-166 · Data inventory and field classification
`Milestone: M-6` · `Builds: N-11` · `Days: 1.0` · `Depends on: T-009` · `Snapshot: var/snapshots/T-166/` · `Build: DEFERRED — out of scope for this window`

**Goal:** every field in the product classified, so retention, encryption, masking and erasure all have something to act on.
**Context:** `spec §N-11.a`: every field appears in the inventory with a class and a retention rule. Generated from the models rather than maintained by hand, because a hand-maintained inventory is wrong within a month.
**Read first:** `src/covenant_radar/db/models/`, `spec §14.2`, `spec §21`.
**Contracts:** the classification `T-167` and `T-168` read.
**Files owned:** `src/covenant_radar/db/classification.py`, `scripts/build_data_inventory.py`, `docs/compliance/data-inventory.md`, `tests/unit/test_data_inventory.py`
**Behaviour:** a classification declared on every column as model metadata; an inventory generated listing table, column, class, retention rule, encryption state and whether it may leave the host; a build check failing on an unclassified column.
**Every case:** a new column without a classification → the build fails naming it, which is what keeps the inventory true; a column classed personal but not encrypted → the check fails, since the classification is a commitment; a column classed as permitted to leave the host but absent from the outbound whitelist → the check fails, so the two lists cannot disagree; the inventory regenerating on every release; a derived column → classified by the strictest class of its inputs.
**Steps:** 1. Add classification metadata to every column. 2. Implement the generator. 3. Implement the consistency checks against encryption and the outbound whitelist. 4. Implement strictest-class derivation. 5. Add the checks to the gate.
**Tests:** `tests/unit/test_data_inventory.py` — `test_every_column_classified`, `test_personal_columns_encrypted`, `test_outbound_permitted_matches_whitelist`, `test_derived_takes_strictest_class`, `test_inventory_regenerates`.
**Run:** `pytest -q tests/unit/test_data_inventory.py` 5 passed · `python scripts/build_data_inventory.py` exit 0.
**Done when:** every column is classified and the inventory agrees with the encryption and whitelist configurations.
**Evidence:** the generated inventory.

---

### T-167 · Retention enforcement job and the purge log
`Milestone: M-6` · `Builds: N-11` · `Days: 1.5` · `Depends on: T-166, T-120` · `Snapshot: var/snapshots/T-167/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the retention schedule is executed rather than documented, and what was purged is recorded.
**Context:** `spec §N-11.b`: the job purges exactly what is due, logs it, and leaves the audit chain valid.
**Read first:** `spec §21`, `src/covenant_radar/db/classification.py`, `audit/chain.py`.
**Contracts:** the purge log `T-115`'s screen shows.
**Files owned:** `src/covenant_radar/services/retention.py`, `src/covenant_radar/scheduler/jobs.py` (the retention job), `docs/compliance/retention.md`, `tests/integration/test_retention.py`
**Behaviour:** a scheduled job purging per the configured schedule, honouring references so nothing still cited is removed, downsampling where the schedule says aggregate rather than delete, writing a purge log per entity, and leaving the audit chain valid.
**Every case:** a record still referenced by a retained record, such as a simulation cited by a memo → not purged, and the reason recorded, because a dangling citation breaks a reconstruction; an audit event past its retention → not purged before the regulatory period, ever, and a test asserts the floor; a downsampling rule → aggregating and recording the aggregation rather than deleting silently; a purge interrupted → resumable with no double counting; a dry run → reporting counts per entity and writing nothing.
**Steps:** 1. Implement the schedule reader from the classification and configuration. 2. Implement reference checking. 3. Implement deletion and downsampling paths. 4. Implement the purge log and the dry run. 5. Assert the audit floor and chain validity.
**Tests:** `tests/integration/test_retention.py` — `test_referenced_record_not_purged_with_reason`, `test_audit_floor_never_breached`, `test_downsampling_records_aggregation`, `test_interrupted_purge_resumable`, `test_dry_run_writes_nothing`, `test_chain_valid_after_purge`.
**Run:** `pytest -q tests/integration/test_retention.py` 6 passed.
**Done when:** the six tests pass and the chain remains valid after a purge.
**Evidence:** a purge log, a chain verification after purge.

---

### T-168 · Erasure procedure with audit-chain preservation
`Milestone: M-6` · `Builds: N-11` · `Days: 1.5` · `Depends on: T-167` · `Snapshot: var/snapshots/T-168/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a data-principal erasure request honoured without destroying the audit trail — the requirement that looks contradictory and is not.
**Context:** `spec §N-11.c`: personal content removed while every affected audit event remains chain-valid and is marked redacted. The chain covers the hash of the payload, so redaction must preserve the hash while removing the content, which is why the payload is stored with a separable personal section.
**Read first:** `src/covenant_radar/audit/chain.py`, `services/retention.py`, `spec §21`.
**Contracts:** the erasure command; `C-60`.
**Files owned:** `src/covenant_radar/services/erasure.py`, `src/covenant_radar/cli.py` (the erasure command), `docs/compliance/erasure.md`, `tests/integration/test_erasure.py`
**Behaviour:** a request recorded as an audit event; personal content replaced with a redaction marker across business records; audit payloads redacted in their separable personal section with the chain hash preserved; documents containing personal data handled per the documented policy; a completion certificate produced.
**Every case:** an erasure request → its own audit events for request and completion, since the erasure itself is an event that must be provable; a chain verified after erasure → still valid, asserted by a test, and this is the property the whole design serves; a record required to be retained by regulation → not erased, with the legal basis recorded and communicated in the certificate; a document that cannot be selectively redacted → handled per the documented policy with the decision recorded; an erasure requested for a subject with no data → a certificate stating so, not an error.
**Steps:** 1. Implement the request and completion events. 2. Implement business-record redaction. 3. Implement audit-payload redaction preserving the chain hash. 4. Implement the regulatory-retention exception with its basis. 5. Implement the certificate. 6. Assert chain validity after erasure.
**Tests:** `tests/integration/test_erasure.py` — `test_chain_valid_after_erasure`, `test_request_and_completion_audited`, `test_regulatory_retention_exception_recorded_and_communicated`, `test_no_data_subject_gets_certificate_not_error`, `test_personal_content_removed_from_business_records`, `test_documents_handled_per_policy`.
**Run:** `pytest -q tests/integration/test_erasure.py` 6 passed.
**Done when:** the six tests pass and the chain verifies after an erasure.
**Evidence:** an erasure certificate, a chain verification after erasure.

---

### T-169 · Compliance evidence pack assembly
`Milestone: M-6` · `Builds: N-11` · `Days: 1.0` · `Depends on: T-168, T-158` · `Snapshot: var/snapshots/T-169/` · `Build: DEFERRED — out of scope for this window`

**Goal:** every regulatory obligation mapped to the control that satisfies it and the passing test that proves it — assembled rather than asserted.
**Context:** `spec §N-11.d`: every row points at a test that exists and passes. This is what turns "we comply" into something a customer's compliance function can verify in an afternoon.
**Read first:** `spec §2.1`, `spec §16.4`, `tests/security/`, `docs/compliance/`.
**Contracts:** none.
**Files owned:** `docs/compliance/evidence-pack.md`, `scripts/build_evidence_pack.py`, `tests/unit/test_evidence_pack.py`
**Behaviour:** a generated pack mapping each obligation in `spec §2.1` to its requirement, its control, its test and that test's most recent result, together with the data inventory, the retention schedule, the purge log summary, the authorization matrix, the model register and the outbound-capture scan.
**Every case:** an obligation with no mapped test → the build fails, because an unmapped obligation is an unmet one until proven otherwise; a mapped test that is failing or skipped → the pack marks it prominently rather than reporting compliance; the pack regenerating from the current test results on every release, never from a stored claim; an artefact referenced but absent → named as absent; the pack readable by a compliance officer without engineering vocabulary.
**Steps:** 1. Encode the obligation-to-control-to-test mapping. 2. Implement generation pulling the latest results. 3. Include the six supporting artefacts. 4. Assert full mapping coverage. 5. Review the language with a non-engineer.
**Tests:** `tests/unit/test_evidence_pack.py` — `test_every_obligation_mapped`, `test_every_mapped_test_exists`, `test_failing_test_marked_prominently`, `test_pack_regenerates_from_current_results`, `test_absent_artefact_named`.
**Run:** `pytest -q tests/unit/test_evidence_pack.py` 5 passed · `python scripts/build_evidence_pack.py` exit 0.
**Done when:** every obligation maps to a passing test and the pack generates.
**Evidence:** the generated pack.

---

### T-170 · Penetration-test support and remediation
`Milestone: M-6` · `Builds: N-04` · `Days: 4.0` · `Depends on: T-158` · `Snapshot: var/snapshots/T-170/` · `Build: DEFERRED — out of scope for this window`

**Goal:** an independent test finds what internal testing did not, and every high and critical finding is closed before release.
**Context:** `spec §N-04.g` and `[OPEN-08]`: this is a hard release gate. The slot is booked at M-0, not requested here, because a test booked at M-6 delays the release by its own lead time.
**Read first:** the security suites, `docs/compliance/`, `deploy/`.
**Contracts:** unchanged, unless a finding requires one, in which case it is a recorded specification change.
**Files owned:** the specific modules each finding names, `docs/compliance/pentest-remediation.md`, `tests/security/test_pentest_regressions.py`
**Behaviour:** a test environment provisioned with representative data and documented scope; findings triaged by severity; every high and critical closed or accepted in writing by a named owner; a regression test per finding so it cannot return.
**Every case:** a finding closed without a regression test → not closed, since a fix with no test is a fix waiting to be reverted; a finding accepted rather than fixed → accepted in writing by a named owner with the reason and the compensating control, never by silence; a finding revealing a specification-level gap → escalated as a change rather than patched at the edge; a medium or low finding → recorded with a decision and a target, not silently deferred; the retest confirming closure before release.
**Steps:** 1. Provision the environment and document the scope. 2. Support the test and triage the findings. 3. Fix every high and critical, each with a regression test. 4. Record acceptances with owners and compensating controls. 5. Escalate specification-level findings. 6. Obtain and record the retest result.
**Tests:** `tests/security/test_pentest_regressions.py` — one test per finding, each named for it.
**Run:** `pytest -q tests/security/test_pentest_regressions.py` all passed · the retest report showing no open high or critical findings.
**Done when:** every high and critical is closed with a regression test, or accepted in writing by a named owner.
**Evidence:** the report, the remediation record, the retest.

---

### T-171 · Release candidate assembly and the acceptance sweep
`Milestone: M-6` · `Supports: spec §23` · `Days: 2.0` · `Depends on: T-170` · `Snapshot: var/snapshots/T-171/` · `Build: DEFERRED — out of scope for this window`

**Goal:** `spec §23`'s ten acceptance criteria executed on a clean install and recorded, so the release decision is made against evidence.
**Context:** `spec §23`: if someone can argue about whether an item passed, the item is written wrongly. Each points at a check, a measured number or an artefact.
**Read first:** `spec §23`, every gate and suite.
**Contracts:** none.
**Files owned:** `docs/release/rc-checklist.md`, `scripts/release_sweep.py`, `CHANGELOG.md`, `docs/release/evidence/*`
**Behaviour:** a clean install of the candidate; every requirement check executed; the full gate; the evaluation scoreboard; the performance table; the accessibility audit and screen-reader walkthrough; the authorization matrix; the outbound scan; the penetration-test closure; the backup and restore rehearsal; the clean install by a non-author; the upgrade and deliberate failed-upgrade rollback; the compliance pack; the documentation review; and a sign-off by a named owner.
**Every case:** any criterion failing → the release does not proceed, and the failure is recorded with an owner and a plan; a criterion waived → waived in writing by a named owner with the reason in the pack, never by omission; the sweep run on the release candidate rather than on the main line, since they are not the same thing; every artefact attached rather than referenced by memory; the changelog written from the merged task list, not from recollection.
**Steps:** 1. Build the candidate from the tag and install it cleanly. 2. Execute every criterion in order, attaching evidence. 3. Record failures with owners. 4. Write the changelog from the merged tasks. 5. Obtain the named sign-off.
**Tests:** `scripts/release_sweep.py` runs the automatable criteria and reports; the manual ones are recorded with their evidence.
**Run:** `python scripts/release_sweep.py` reporting every criterion · every manual criterion recorded.
**Done when:** all ten criteria are true and signed off, or the release is held with a recorded reason.
**Evidence:** the completed checklist with its attached evidence.

---

### T-172 · Evaluation build packaging and the walkthrough script
`Milestone: M-6` · `Supports: spec §22` · `Days: 1.5` · `Depends on: T-171` · `Snapshot: var/snapshots/T-172/` · `Build: DEFERRED — out of scope for this window`

**Goal:** a prospective customer forms a judgement in a day, on their own hardware, without their data and without a network.
**Context:** `spec §22.1`: installs in under an hour, requires no customer data and no outbound network, and demonstrates every capability with the model stages running against recorded responses where no provider is configured.
**Read first:** `spec §22.1`, `spec §22.2`, `evaluation/reference_portfolio/`, `deploy/`.
**Contracts:** `C-72`.
**Files owned:** `deploy/evaluation/*`, `docs/evaluation/walkthrough.md`, `docs/evaluation/questions.md`, `tests/integration/test_evaluation_build.py`
**Behaviour:** a packaged build that installs, seeds the reference portfolio, runs a scoring pass, fills the cassettes and opens on a populated queue; a walkthrough document following `spec §22.1`'s order; an answers document covering the eight questions in `spec §22.2` with the section each answer comes from.
**Every case:** installed with no network → every capability demonstrable, with the model stages replaying cassettes and **labelled as replay on screen**, because an evaluator must never be shown a replay believing it live; installed with a provider configured → the model stages running live, labelled accordingly; the reference portfolio labelled as synthetic on every screen where it could be mistaken for real; the walkthrough completing inside the stated time, timed by a non-author; the eight answers each citing the section that supports them.
**Steps:** 1. Package the build with the portfolio, cassettes and a pre-computed scoring run. 2. Implement the replay labelling. 3. Label the synthetic portfolio. 4. Write the walkthrough and time it with a non-author. 5. Write the answers document with section citations.
**Tests:** `tests/integration/test_evaluation_build.py` — `test_installs_and_opens_populated_with_no_network`, `test_replay_labelled_on_screen`, `test_synthetic_portfolio_labelled`, `test_every_capability_reachable_in_walkthrough`, `test_walkthrough_completes_within_time`.
**Run:** `pytest -q tests/integration/test_evaluation_build.py` 5 passed · a timed walkthrough by a non-author.
**Done when:** the five tests pass and a non-author completes the walkthrough in the stated time.
**Evidence:** the timed walkthrough record, the packaged build.

---

### T-173 · Integration, stabilisation and release-engineering reserve
`Milestone: M-6` · `Supports: all` · `Days: 8.0` · `Depends on: —` · `Snapshot: var/snapshots/T-173/` · `Build: DEFERRED — out of scope for this window`

**Goal:** the named reserve that absorbs integration work at each milestone boundary and the release engineering that no single task owns.
**Context:** `plan.md §10.4` line (c). This is **not** slack and not a place to put work that should have been a task. It is drawn against by name, with the reason recorded, and the drawdown is reported at every gate.
**Read first:** `MERGE_LOG.md`, the gate records.
**Contracts:** unchanged.
**Files owned:** whatever a specific drawdown names, recorded before the work starts.
**Behaviour:** each drawdown recorded in `MERGE_LOG.md` with the milestone, the reason, the days and the files touched; typical uses are cross-task integration defects found at a gate, flakiness in a suite, environment differences between development and CI, and the release-engineering steps that span tasks.
**Every case:** a drawdown for work that is a new capability → refused; that is a specification change under `spec §29`, not a reserve draw; a drawdown with no recorded reason → refused; more than a third of the reserve consumed before M-3 → `plan.md §12`'s re-planning trigger, raised with the customer rather than absorbed; a drawdown that fixes a defect → the defect gets a regression test like any other; the reserve exhausted → the remaining milestones re-forecast from the observed ratio, not from the original estimate.
**Steps:** 1. Record the drawdown before starting. 2. Do the work under the standing prohibitions and acceptance criteria. 3. Add a regression test for any defect fixed. 4. Report the cumulative drawdown at the next gate.
**Tests:** whatever the specific work requires; a defect fix always adds a regression test.
**Run:** `python -m radarctl gate` green after every drawdown.
**Done when:** the drawdown is recorded, the work is green, and the cumulative figure is reported at the next gate.
**Evidence:** the drawdown entries in `MERGE_LOG.md`.

---

## §6 Continuous and final verification

### 6.1 The gate, step by step

`python -m radarctl gate` runs these in order, stopping at the first failure and exiting with that step's code. `--fast` runs steps 1–6.

| # | Step | Expected |
|---|---|---|
| 1 | `ruff format --check src tests evaluation scripts` | exit 0, no output |
| 2 | `ruff check src tests evaluation scripts` | exit 0 |
| 3 | `mypy src` | exit 0, strict on `domain` and `services` |
| 4 | `lint-imports` | exit 0, all six layer contracts hold |
| 5 | `pytest -q tests/unit tests/property` | exit 0, coverage at or above threshold |
| 6 | `radarctl migrate check` | exit 0, no model drift |
| 7 | `pytest -q tests/integration` | exit 0, against a real PostgreSQL instance |
| 8 | `pytest -q tests/contract tests/migration` | exit 0, no API or schema drift |
| 9 | `pytest -q tests/security` | exit 0, authorization matrix and scope leakage clean |
| 10 | `radarctl seed --check-deterministic` | exit 0, identical content hashes |
| 11 | `python -m evaluation.run --both-arms --gate` | exit 0, no score below its floor |
| 12 | `pytest -q tests/e2e tests/a11y` | exit 0, both themes, both languages, zero accessibility violations |
| 13 | `bandit -r src` · `pip-audit` · `detect-secrets scan` | exit 0, nothing at or above the configured severity |
| 14 | `radarctl perf --check` | exit 0, or a documented miss |
| 15 | prompt-hash, threshold-literal, design-literal, i18n-literal, data-inventory and documentation-coverage checks | exit 0 |

Every step runs with no network access. A step whose implementing task is not yet built prints `SKIP <step> — T-0NN` and exits zero, so the gate is runnable from `T-002` onward.

### 6.2 Verification that is a person doing something

Six checks cannot be automated, and each has an owner, a schedule and a recorded artefact rather than an assumption:

| Check | Who | When | Artefact |
|---|---|---|---|
| Visual review against the design direction and refusals | A reviewer who did not build the screen | Every interface task, and at every milestone gate that touches the interface | A signed note in `MERGE_LOG.md` |
| Screen-reader walkthrough of the primary flows | An accessibility reviewer | Before each release | A recording and a findings list |
| Clean install from the guide alone | Someone who did not write the installer | Before each release | An install transcript and the verification output |
| Backup restore rehearsal | An administrator | Quarterly and before each release | A timed restore record |
| Penetration test and retest | An external vendor | Before each release | The report and the remediation record |
| Usability sessions with the customer's own staff | A facilitator who did not build the product | At M-3's gate and again in pilot | Session notes against `spec §6`'s G4 marks |

### 6.3 Fresh-install verification, which never touches a working system

Every install, upgrade and restore test runs against a disposable host or container. No verification procedure in this document modifies an existing deployment, and the upgrade tests run against a copy of a representative database rather than a live one. A procedure that would touch production is a procedure that will eventually be run against production by mistake.

---

## §7 What proves what

### Table 1 — every requirement in `spec §10`

| Requirement | Tasks | Verified by |
|---|---|---|
| R-01 data model and migrations | T-006 … T-011 | `test_model_*`, `tests/migration/test_initial.py`, `radarctl migrate check` |
| R-02 master data | T-008, T-010, T-023 | `test_master_data.py`, `test_model_borrower.py` |
| R-03 statement ingestion | T-024, T-025, T-026 | `test_statement_import.py`, `test_restatement.py`, `test_chart.py` |
| R-04 document ingestion and OCR | T-084 … T-087 | `test_document_upload.py`, `test_native_extraction.py`, `test_ocr.py`, `test_document_viewer.py` |
| R-05 covenant registry | T-031, T-032, T-033 | `test_registry_versioning.py`, `test_exceptions_cure.py`, `test_waivers.py`, `test_registry_service.py` |
| R-06 verified intake | T-093 … T-096 | `test_verification.py`, `test_verification_closed.py`, `test_intake_service.py`, `test_confirm_refusal.py` |
| R-07 ratio library | T-027 … T-030 | `test_ratios_1.py`, `test_ratios_2.py`, `test_custom_formula.py`, `test_not_computable.py` |
| R-08 covenant engine | T-034 … T-037 | `test_engine.py`, `test_headroom_invariants.py`, `test_calendar.py`, `test_sma.py`, `test_stage2_trace.py` |
| R-09 certificate workflow | T-038, T-039 | `test_certificate_generation.py`, `test_certificate_lifecycle.py` |
| R-10 signal ingestion | T-042 … T-045 | `test_signal_ingestion.py`, `test_late_arrival.py`, `test_ingestion_report.py` |
| R-11 evidence ledger | T-046 … T-051 | `test_persistence.py`, `test_materiality.py`, `test_decay.py`, `test_supersession.py`, `test_revision.py`, `test_stage3_trace.py` |
| R-12 horizon forecast | T-052 … T-056 | `test_projection.py`, `test_crossing.py`, `test_probability.py`, `test_confidence.py`, `test_forecast_persistence.py`, `test_cohort_dating.py` |
| R-13 driver attribution | T-057, T-058 | `test_attribution.py`, `test_attribution_invariants.py`, `test_stage4_trace.py` |
| R-14 portfolio triage | T-059 … T-061 | `test_urgency.py`, `test_ordering_invariants.py`, `test_what_changed.py`, `test_queue_query.py` |
| R-15 intervention simulation | T-062 … T-064 | `test_effect_models.py`, `test_simulation.py`, `test_simulation_comparison.py`, `test_simulation_persistence.py` |
| R-16 action catalogue | T-098 | `test_catalogue.py` |
| R-17 grounded memo | T-099 … T-102 | `test_memo_slots.py`, `test_memo_shapes.py`, `test_memo_refusal.py`, `test_memo_export.py` |
| R-18 case management | T-109, T-110 | `test_case_lifecycle.py`, `test_case_service.py`, `test_case_screens.py` |
| R-19 override and disposition | T-111, T-112 | `test_overrides.py`, `test_dispositions.py` |
| R-20 audit and reconstruction | T-066 … T-069 | `test_audit_chain.py`, `test_audit_store.py`, `test_audit_coverage.py`, `test_reconstruction.py`, `test_evidence_bundle.py` |
| R-21 explainability | T-070 … T-072 | `test_trace_reader.py`, `test_why_panel.py`, `test_why_standalone.py`, `test_explain_contract.py` |
| R-22 queue screen | T-073, T-074 | `test_queue_screen.py`, `test_queue_filters.py`, `test_queue_performance.py` |
| R-23 case file | T-075 … T-078 | `test_case_file.py`, `test_forecast_panel.py`, `test_horizon_api.py`, `test_horizon_control.py`, `test_evidence_margin.py` |
| R-24 intake screen | T-097 | `test_intake_screen.py`, `test_intake_flow.py` |
| R-25 audit and governance screens | T-079 … T-081 | `test_simulator_screen.py`, `test_audit_screens.py`, `test_governance_screens.py` |
| R-26 administration console | T-113 … T-115 | `test_admin_users.py`, `test_privilege_escalation.py`, `test_admin_config.py`, `test_admin_ops.py` |
| R-27 notifications | T-116 … T-119 | `test_notification_scope.py`, `test_email_digest.py`, `test_webhooks.py`, `test_inapp_notifications.py` |
| R-28 batch orchestration | T-120 … T-122 | `test_scheduler.py`, `test_nightly_pipeline.py`, `test_batch_resilience.py` |
| R-29 connectors | T-123 … T-127 | `test_connector_framework.py`, `test_connectors_read_only.py`, `test_file_drop.py`, `test_rest_pull.py`, `test_db_view.py`, `test_connector_reconciliation.py` |
| R-30 external feeds | T-128 … T-131 | `test_synthetic_feed.py`, `test_feed_adapters.py`, `test_entity_resolution.py`, `test_match_review.py`, `test_feed_dedupe.py` |
| R-31 regulatory reporting | T-132 … T-134 | `test_crilc.py`, `test_rfa_pack.py`, `test_mis.py` |
| R-32 REST API | T-135, T-136 | `test_api_resources.py`, `test_api_contract.py`, `test_api_keys.py` |
| R-33 search and saved views | T-137, T-138 | `test_search.py`, `test_search_scope.py`, `test_saved_views.py` |
| R-34 bulk operations | T-139 | `test_bulk_operations.py` |
| R-35 languages | — | **Requirement withdrawn.** `T-140` and `T-141` are removed; English renders directly. |
| R-36 theming | T-082 | `test_theme.py`, `test_contrast_both_themes.py` |
| N-01 evaluation harness | T-103 … T-106 | `test_examples.py`, `test_harness_product.py`, `test_harness_baseline.py`, `test_score_floors.py` |
| N-02 observability | T-142 … T-145 | `test_log_redaction.py`, `test_log_contents.py`, `test_observability_endpoints.py`, `test_correlation.py`, `test_alert_rules.py` |
| N-03 configuration | T-004, T-012 | `test_settings.py`, `test_thresholds.py`, `test_threshold_approval.py` |
| N-04 security | T-013 … T-019, T-170 | `test_auth_local.py`, `test_auth_sso.py`, `test_permissions.py`, `test_scope_leakage.py`, `test_crypto.py`, `test_maker_checker.py`, `test_hardening.py`, `test_pentest_regressions.py` |
| N-05 performance | T-146, T-147 | `tests/perf/*`, `radarctl perf --check` |
| N-06 reliability | T-148 … T-150 | `test_backup_restore.py`, `test_lifecycle.py`, `test_integrity_checks.py` |
| N-07 accessibility | T-083 | `tests/a11y/*` |
| N-08 deployability | T-151 … T-155 | `test_install_linux.py`, `test_install_windows.py`, `test_upgrade.py`, `test_container.py`, `test_build_reproducibility.py` |
| N-09 test strategy | T-156 … T-160 | `tests/property/*`, `tests/migration/*`, `test_authorization_matrix.py`, `tests/e2e/*`, `test_coverage_gates.py` |
| N-10 documentation | T-161 … T-165 | `test_documentation.py`, the observed install |
| N-11 compliance | T-166 … T-169 | `test_data_inventory.py`, `test_retention.py`, `test_erasure.py`, `test_evidence_pack.py` |
| N-12 model governance | T-107, T-108 | `test_model_registry.py`, `test_drift.py` |

**Every requirement has at least one task and at least one executable check. Every task builds a requirement that exists or supports a section that does.**

### Table 2 — every failure case in `spec §19`

| Case | Task | Test |
|---|---|---|
| Empty or oversized clause input | T-095, T-097 | `test_verification.py`, `test_intake_screen.py` |
| Instruction-injection in text sent to a model | T-095 | `test_verification_closed.py::test_injection_refused_and_audited` |
| Hostile input in any field | T-019 | `test_hardening.py` |
| Malformed or truncated import file | T-025 | `test_statement_import.py::test_bad_row_quarantined_rest_load` |
| Duplicate delivery of a file or batch | T-025, T-042, T-124 | `test_reimport_is_idempotent`, `test_duplicate_counted_not_errored`, `test_duplicate_file_skipped_with_note` |
| Source schema changed | T-127 | `test_connector_reconciliation.py` |
| Missing quarter for a borrower | T-034 | `test_engine.py::test_incomplete_period_marks_stale_naming_last` |
| Ratio undefined | T-030, T-034 | `test_not_computable.py`, `test_engine.py` |
| Password-protected or corrupt document | T-085 | `test_native_extraction.py::test_damaged_pdf_refused_naming_page` |
| OCR below the confidence floor | T-086 | `test_ocr.py::test_below_floor_flagged_and_excluded_from_detection` |
| Ambiguous external entity match | T-130 | `test_entity_resolution.py::test_middle_band_queued_and_item_held` |
| Model provider slow, down or returning junk | T-089, T-101 | `test_call_site.py`, `test_memo_refusal.py` |
| Model budget or rate ceiling reached | T-089 | `test_call_site_ceilings.py` |
| Notification delivery failure | T-118 | `test_webhooks.py::test_three_failures_dead_letter_and_alert` |
| Batch job failure and deadline miss | T-122 | `test_batch_resilience.py` |
| Database unavailable | T-149 | `test_lifecycle.py::test_database_blip_opens_and_closes_circuit` |
| Disk full or log directory unwritable | T-142 | `test_log_contents.py::test_unwritable_directory_does_not_break_request` |
| Concurrent edit of the same record | T-023 | `test_master_data.py::test_stale_version_conflict_names_change` |
| Session expiry mid-form | T-013 | `test_auth_local.py::test_expired_session_preserves_destination` |
| Crash or restart mid-batch | T-120, T-149 | `test_scheduler.py`, `test_lifecycle.py::test_hard_kill_leaves_no_partial_state` |
| Retention purge encountering referenced data | T-167 | `test_retention.py::test_referenced_record_not_purged_with_reason` |
| Clock skew detected | T-149 | `test_lifecycle.py` startup self-check |

### Table 3 — every risk in `spec §26` whose mitigation is work

| Risk | Mitigating tasks | Early-warning signal |
|---|---|---|
| RISK-01 probabilities not believed | T-054, T-071, T-105, T-065 | Override rate alert (T-145) |
| RISK-02 noise escalated | T-047, T-048, T-049, T-065, T-106 | Amber-share alert (T-145) |
| RISK-03 extraction accuracy on real documents | T-086, T-093, T-095, T-097 | Field precision during onboarding (T-104) |
| RISK-04 customer data quality | T-026, T-045, T-127, T-034 | Quarantine depth alert (T-145) |
| RISK-05 provider availability or terms | T-088, T-091, T-089 | Provider latency and budget alerts (T-145) |
| RISK-06 single-host capacity outgrown | T-146, T-147 | Batch duration trend (T-145) |
| RISK-07 regulatory change | T-012, T-098, T-132, T-167 | Annual compliance review (T-169) |
| RISK-08 scope growth in pilot | T-123, T-128, T-098, T-132, T-012 | Change requests during pilot |
| RISK-09 adoption failure | T-117, T-109, T-083, T-162 | Login and disposition telemetry (T-143) |
| RISK-10 key-person dependency | T-165, and every task's tests | Review backlog in `MERGE_LOG.md` |

RISK mitigations that `spec §26` marks as posture rather than work — advisory-only outputs, transparency of the formula, honest labelling — are properties of tasks already listed and carry no separate task.

---

## §8 What remains open

### 8.1 Open questions inherited from the specification

| ID | Question | Owner | Blocked | Default |
|---|---|---|---|---|
| [OPEN-01] | Freedom-to-operate patent search | Legal | Commercial launch, not engineering | Claim combination novelty only |
| [OPEN-02] | Model provider commercial envelope | Customer / provider | The published per-borrower cost figure | Assume tight: ceilings on, cost logged and surfaced |
| [OPEN-03] | Identity provider and test metadata | Customer IT | SSO configuration only | Local store; SSO configured at deployment (T-014 built either way) |
| [OPEN-04] | Core-banking and LOS extract layouts | Customer IT | The customer-specific mapping | Generic layouts; map at deployment (T-124 built against generics) |
| [OPEN-05] | Licensed news and industry feed | Customer procurement | That family's live source | Synthetic generator (T-128); adapters tested on fixtures (T-129) |
| [OPEN-06] | Customer calibration data | Risk head, in pilot | Calibrated probabilities on real accounts | Reference-portfolio calibration (T-065), labelled as such |
| [OPEN-07] | SMTP relay and webhook endpoints | Customer IT | Email and webhook delivery | In-app notifications (T-119); configure at deployment |
| [OPEN-08] | Penetration-test vendor and slot | Security | Release sign-off | **No default — a hard gate.** Book at M-0 |
| [OPEN-09] | Retention periods the customer requires | Customer compliance | The configured schedule | Regulatory minimums (T-167) |
| [OPEN-10] | Statistical stage-4 challenger required? | Risk head | Nothing — off by default | Deterministic forecaster remains champion |

### 8.2 Open questions arising in the plan

| ID | Question | Owner | Blocked | Default |
|---|---|---|---|---|
| **[OPEN-11]** | Is version control available? | Engineering | `T-001` | **Resolved: no. §3's snapshot protocol replaces it.** Install per-user git; the snapshot fallback is worse in every way and is a reason to escalate |
| **[OPEN-12]** | Which PostgreSQL instance does CI use? | Engineering | `tests/integration` in CI | A local service on the build machine, with the suite failing rather than silently skipping (T-003) |
| **[OPEN-13]** | Is Tesseract installable on the target hosts, and under which terms? | Engineering with the customer | `T-086` OCR | Native extraction only; scanned pages route to review; the limitation documented |
| **[OPEN-14]** | Customer retention periods beyond the 8-year default? | Customer compliance | `T-167` values only | The documented minimums |
| **[OPEN-15]** | Which font families are licensed for self-hosting, with Devanagari coverage? | Product with the customer | `T-020`'s final asset set | Metric-compatible open families with recorded licences |
| **[OPEN-16]** | Does the customer require a model-risk sign-off before enabling the model stages? | Customer risk | Enablement, not the build | Ship with model stages disabled; enable on written approval, which the capability switch supports |

None is a build blocker with its default taken, and every default is a configuration change later rather than a rebuild. **[OPEN-11] is the exception in urgency**: it is not a blocker for the code, but it is a blocker for the process, and taking its default means working without history.

### 8.3 Disagreements with the specification, recorded and planned as written

1. **Per-requirement effort differs from `spec §10`'s figures in both directions.** The largest gaps are N-08 deployability (5 → 8), R-29 connectors (7 → 8), R-20 audit (5 → 6.5) and N-04 security (8 → 9.5). Requirements are unchanged and the milestone totals still reconcile to 272.
2. **Three tasks sit outside the milestone their requirement belongs to.** The provider layer is in M-4 with the work that first needs it; the reference portfolio is in M-1 because M-2's gate cannot be demonstrated without it; calibration closes M-2 rather than opening M-3. Each is sequencing, not scope.
3. **`spec §12.3`'s critical path omits the tooling prefix and the release tail.** `plan.md §10.2` adds both.
4. **Accessibility is spent continuously rather than in one block.** `T-021` carries it from the first component, and `T-083` closes gaps rather than creating compliance.

### 8.4 Supporting work the specification did not itemise

Tooling and CI (`T-001`–`T-003`, 3.0) · core primitives (`T-005`, 1.5) · design tokens and components (`T-020`, `T-021`, 3.5) · application shell (`T-022`, 1.5) · reference portfolio (`T-040`, `T-041`, 4.5) · calibration (`T-065`, 3.0) · provider layer, call site, masking, cassettes and prompts (`T-088`–`T-092`, 6.5) · release and evaluation build (`T-171`, `T-172`, 3.5) · integration reserve (`T-173`, 8.0). **Total 35.0 across 18 tasks. Nothing else is built.** A task proposing anything not in this list and not in `spec §10` stops and prints the refusal line.

---

## §9 The release gate

Before a release ships, all ten of `spec §23`'s criteria are true on a clean install of the release candidate, each with attached evidence and a named owner's sign-off. `T-171` executes the sweep; this is the checklist it works from.

1. **Every requirement check passes.** R-01 … R-36 and N-01 … N-12, by the methods in §7 Table 1. Any waiver is in writing from a named owner.
2. **Quality gates green.** All fifteen steps of §6.1 on the release candidate, including coverage, mutation and the accessibility audit.
3. **Evaluation scored and published.** Both arms on the full example set, every pass mark met or the miss documented with an owner, and the scoreboard in the release notes.
4. **Security signed off.** Penetration test complete with every high and critical closed or accepted in writing; dependency, secret and image scans clean; the authorization matrix generated and matching the specification; the outbound capture showing zero personal fields and zero secret material; TLS verification provably not disableable.
5. **Compliance pack complete.** Every obligation in `spec §2.1` mapped to a control, a requirement and a passing test; data inventory, retention schedule, purge log, erasure procedure and model register current.
6. **Operations proven.** Clean install on a fresh host by a non-author within the documented time; upgrade from the prior release preserving data; a deliberately failed upgrade rolling back automatically; a timed backup restore inside the recovery objective; every alert with a runbook section.
7. **Performance measured.** Every `spec §18` row measured on the reference hardware and published, including any miss.
8. **Interface proven.** WCAG 2.2 AA clean on every screen in both themes and both languages; the screen-reader walkthrough recorded and its findings closed; usability sessions run with at least five participants from the customer's desk against `spec §6`'s G4 marks.
9. **Documentation complete.** Administrator guide, user guides, API reference, runbook, decision records, model cards and release notes, each reviewed by someone who did not write it.
10. **Evaluation build reproducible.** The reference portfolio regenerates deterministically, the evaluation build installs from the tag, and `spec §22.1`'s walkthrough completes end to end within its stated time.

**If someone can argue about whether an item passed, the item is written wrongly.** Each points at a check, a measured number or an artefact that exists or does not.

---

## §10 Checks on this document

1. **OK** — every requirement in `spec §10` has at least one task (§7 Table 1, no empty cell), and every one of the 173 tasks carries `Builds:` a requirement that exists or `Supports:` a section that does. There is no task without a purpose and no requirement without a task.
2. **OK** — the 173 blocks' `Days` fields sum, milestone by milestone, to M-0 34.0, M-1 29.0, M-2 35.0, M-3 29.0, M-4 39.0, M-5 51.0, M-6 55.0 = **272.0**, reconciling with `plan.md §10.4` line (d) and `spec §28.1`'s subtotal before contingency. 155 blocks carry `Builds:` (237.0 days) and 18 carry `Supports:` (35.0 days), matching `plan.md §10.4`'s lines (a), (b) and (c).
3. **OK** — every task is between 0.5 and 3.0 days except `T-170` (4.0, an external dependency whose scope is not ours to split) and `T-173` (8.0, a named reserve drawn against in pieces). Both exceptions are stated in their blocks.
4. **OK** — every block carries goal, context, read-first paths, contracts, owned files, behaviour, an `Every case` list covering the wrong, empty, hostile, duplicate, absent and degraded paths, ordered steps, named tests, runnable commands with expected results, a done-when and its evidence. No block contains "as appropriate" or "decide during implementation".
5. **OK** — the standing prohibitions and standing acceptance criteria are stated once in §0 and referenced rather than repeated 173 times, so a block is readable and the rules cannot drift between copies.
6. **OK** — every task's `Depends on` names only earlier tasks, and §2.3's critical chain is consistent with the dependency lists. There is no cycle.
7. **OK** — the execution model is one agent throughout. No lane, batch, write-set disjointness proof, cross-agent fence or assistant-capacity window appears anywhere in this document, and §3 is the whole protocol.
8. **OK** — contracts are frozen in `plan.md §6` and referenced by id; no block invents one, and §1's three standing answers mean no block re-derives identity, error mapping or time and money handling.
9. **OK** — the never-permitted operations appear as structural properties with tests: `T-096` and `T-158` assert that no role in any configuration may confirm a failed proposal, and `T-135` and `T-158` assert that no route exists for a credit decision inside the tool.
10. **OK** — every failure case in `spec §19` maps to a task and a named test (§7 Table 2), and every risk in `spec §26` whose mitigation is work maps to tasks and an early-warning signal (§7 Table 3).
11. **OK** — there is no cut list, no narrowed variant, no `should`, no `later` and no `LATER:` in this document, because `spec.md` v1.0 has none. `plan.md §12` states what happens instead when a milestone runs long: split, draw the reserve by name, or re-plan with the customer.
12. **OK** — every suite runs with no network access, enforced by the outbound guard `T-003` installs, and that is the same property that lets the product run air-gapped.
13. **OK** — the six manual verifications in §6.2 each have an owner, a schedule and a recorded artefact, so nothing that cannot be automated is left to assumption.
14. **OK** — [OPEN-11] is resolved: there is no version-control system on this machine, and §3's snapshot protocol plus the `MERGE_LOG.md` ledger provide reversibility and the audit of who built what, without branch-per-task.
15. **OK** — effort figures are estimates and are labelled as such. `MERGE_LOG.md` records actuals from the first merge and `plan.md §12` checks the trend every fifth merge, so estimate quality is visible by M-1 rather than at M-6.

*One honest residual under the OK verdicts.* §7's tables prove that every requirement has a task and a test, not that the tests are sufficient. Sufficiency is what `T-156`'s property tests, `T-160`'s mutation score, `T-158`'s exhaustive authorization matrix and `T-170`'s external penetration test exist to probe — and each of those can itself be wrong. The honest claim is that the coverage is systematic and its gaps are visible, not that it is complete.

*(End of tasks.md — companion to spec.md v1.0 and plan.md, 2026-08-30.)*
