# spec.md — Covenant Radar

**AI-Powered Dynamic Covenant Monitoring & Early Warning · Commercial Banking · Credit Risk**
**Product Specification v1.0 (GA) · 2026-08-30**

This document specifies a production system that a bank installs, operates and is audited on. It is not a prototype specification, and it contains no feature deferrals: every capability named in `problem.md` is built for the 1.0 general-availability release.

Sections §0–§31. IDs: `R-nn` functional requirements · `N-nn` non-functional requirements · `R-nn.a` verifiable checks · `F-nn` user flows · `P-nn` product-posture exclusions (deliberate, permanent, not deferrals) · `RISK-nn` risks · `[OPEN-nn]` open questions (§27) · `[ASSUMED-nn]` assumptions (§12) · `M-n` delivery milestones (§28). Outcome labels: `MEASURED` (observed on a real run), `PILOT` (to be measured in customer pilot), `TARGET` (a committed engineering target), `ESTIMATED` (reasoned, unmeasured).

**What changed from the 0.1 draft, stated plainly.** Version 0.1 was written for a 24-hour hackathon and carried nine `NG-` exclusions, a four-rung cut ladder, three "narrower versions" of its own headline features, two `LATER:`-only requirements and ~40 deferral markers. All of that is deleted. Authentication, authorization, document/PDF ingestion, real external feeds, core-banking connectors, alert delivery, scheduled batch scoring, what-if simulation, model governance, multi-language UI and theming are **requirements of this release**, each with hours, checks and acceptance criteria. The only exclusions that survive are the five in §8 that are permanent product posture — the things this product must never do because a regulator, not a schedule, says so.

---

## §0 Summary

Covenant Radar is the covenant-monitoring and early-warning system for the relationship, credit and risk teams that watch corporate loan accounts in Indian banks. Today covenants are checked when the borrower's quarterly statements arrive, so a company can slide for months while its file still says compliant. The slide surfaces late, when choices have narrowed and recovery is poorest — lenders took a 67% haircut on claims resolved under insolvency to September 2025. Covenant Radar watches every account every day and warns 30, 60 and 90 days before a covenant is likely to break, with the evidence attached and every step of the reasoning open to an auditor.

It works in ordered stages, and most are plain computation, not AI. Covenant terms enter the system from the sanction letter itself — a PDF, a scanned certificate or pasted text — where a language model proposes the structured fields and code independently re-verifies every one of them before a credit officer confirms it. From then on code computes the ratios, tests each covenant at its contractual frequency with its exceptions and cure periods, scores daily payment, utilisation, treasury, concentration and external signals as sustained or noise, and projects each covenant's headroom to a dated breach risk with named drivers. A risk officer can simulate an intervention and watch the projected date move. The model writes only the final warning memo, from those computed facts. The bank gets a ranked watchlist that refreshes overnight, memos and regulatory packs it can file, alerting into the channels its desks already use, connectors into its own core-banking and loan-origination systems, and an audit trail that reconstructs any warning it ever raised.

The rest of this page, briefly:

- **The business problem:** AI-driven covenant breach early warning over 30/60/90-day horizons for commercial lending (§1), the single case `problem.md` states.
- **Why it works:** SMA-2 stress is a documented lead indicator of NPA slippage about two quarters ahead, and manual quarterly monitoring is the documented gap (§2 rows 5, 8; §3 reasoning).
- **What is distinctive:** a covenant registry whose contract arithmetic the system re-implements and re-verifies rather than storing a number a human typed; a persistence-and-materiality-filtered evidence ledger that suppresses noise instead of amplifying it; a forecast that outputs a *date*; and per-stage explainability that opens the code stages, not only the model call (§4, §5, §17).
- **What it does:** ingest financial statements and documents, digitise and version covenants, test them deterministically, score signals for persistence and materiality, forecast dated breach risk with drivers, simulate interventions, rank the portfolio, draft grounded memos, deliver alerts, export regulatory packs, and keep a complete audit trail (§8, §10).
- **What it deliberately never does:** decide a credit action, approve a waiver, classify fraud, originate or score a loan at sanction, or act without a human (§8 `P-01`…`P-05`). These are permanent posture, required by the RBI framework this product sits inside.
- **Built with (from §13):** Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · PostgreSQL 17 · Jinja2 + HTMX + hand-written CSS and inline SVG · APScheduler · structlog + Prometheus · a pluggable LLM provider layer defaulting to the TCS GenAI Lab gateway.
- **Where it runs:** one hardened host the customer owns — no containers required, no external SaaS dependency for the mechanism, the model gateway the only optional outbound hop (§11, §25).
- **How we know it works:** §17's evaluation harness scores the product against a documented baseline on a versioned example set at every commit; §6's metrics are measured on that harness and again in customer pilot; §23 is the GA definition of done.

---

## §1 Purpose

### 1.1 The problem, plainly

Indian banks monitor loan covenants from borrower-submitted quarterly statements — QIS Forms I–III, stock statements, MSOD returns — reviewed by hand (§2 row 5). Deterioration between reporting cycles is caught late, and the later it is caught the less is recovered: a 67% haircut on admitted claims once accounts reach insolvency (§2 row 3). The bank pays, then depositors and the exchequer via recapitalisation.

Covenant Radar gives the credit team a daily, evidence-backed, dated warning — 30, 60 and 90 days ahead — so intervention happens while a covenant conversation is still cheap.

### 1.2 Why this shape of product, and not the two obvious alternatives

Three product concepts were evaluated against the business case. The mechanism stages are listed in order; ✱ marks a stage decided by code rather than by a prompt.

| # | Concept | Mechanism stages | Prior-art gap (§2/§4) | Verdict |
|---|---|---|---|---|
| **C1** | **Covenant Radar** — registry-first monitoring pipeline | 1 model proposes covenant fields, code re-verifies✱, human confirms · 2 compute ratios, test covenants✱ · 3 score signals: sustained vs noise✱ · 4 project headroom to dated breach risk✱ · 5 simulate interventions✱ · 6 rank portfolio by urgency✱ · 7 model drafts grounded memo | Incumbents track dates or alert on external data; none found combining deterministic recompute, dated horizon, intervention simulation and per-stage audit (§4) | **Chosen** |
| C2 | **Case-File Copilot** — ask-the-loan-file conversational analyst | 1 ingest and index documents✱ · 2 retrieve evidence✱ · 3 model answers with citations · 4 log Q&A✱ | Thin — 9fin ships this shape for debt documents (§4) | Rejected: the 30/60/90 numbers would have to come from the model, which §17's grounding rule forbids and `problem.md`'s deterministic-calculation requirement excludes |
| C3 | **Stress War-Room** — simulation-first breach war-gaming | 1 compute ratios, test covenants✱ · 2 model maps stress narrative to shocks, code applies✱ · 3 simulate paths✱ · 4 re-simulate under interventions✱ · 5 model drafts scenario memo | Borrower-level covenant war-gaming is rarer than portfolio stress testing | Rejected as the *organising* concept; its strongest idea — intervention simulation — is **absorbed into C1 as R-15**, not discarded |

C3's simulation is in this product. C2's document question-answering is not, because a conversational surface over a loan file is a different product with a different failure mode, and this one's answers must be reconstructable rather than plausible.

### 1.3 What this product is not

Nearby problems deliberately unsolved, each because it is someone else's product or someone else's regulated act:

- Not retail or MSME collections and dunning.
- Not fraud adjudication. Radar surfaces signals a Red-Flagged-Account process consumes (§2 law block, R-31), but classifying an account as fraud is a human, regulated decision.
- Not loan origination, underwriting or credit scoring at sanction time.
- Not an external credit rating or a substitute for one.
- Not a document-management system, though it ingests and retains the documents it parses (R-04).
- Not market, liquidity or operational risk monitoring.
- Not IFRS-9/ECL provisioning computation. Radar's outputs feed provisioning inputs; it does not compute the provision.
- Not a chat assistant over bank policy.

---

## §2 Background, market and regulatory context

Research conducted 2026-08-29 with live web access; 41 distinct searches and page-opens. Findings that shape this specification:

| # | Finding | The number | Source | Year | What it changes here |
|---|---|---|---|---|---|
| 1 | Gross NPAs of scheduled commercial banks are at a multi-decadal low — the value proposition is keeping them low, not fighting a fire | GNPA 2.3% (Mar 2025); 2.15% domestic ops (Sep 2025) | RBI FSR via [Business Standard](https://www.business-standard.com/finance/news/indian-scheduled-commercial-banks-gross-npa-ratio-at-15-year-low-125070101394_1.html); [PIB](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2225442&reg=3&lang=1) | 2025 | §1 framing; §6 metrics measure prevention, not cleanup |
| 2 | Loan-portfolio frauds surged even as case counts fell — late detection of borrower misbehaviour is the expensive kind | Loan-related frauds ₹33,148 crore in FY25 (of ₹36,014 crore total, up 194%) | RBI Annual Report via [Business Standard](https://www.business-standard.com/finance/news/bank-fraud-amount-triples-in-fy25-despite-drop-in-number-of-cases-rbi-125052900696_1.html) | 2025 | §6 value anchor; justifies treasury-flow and diversion signals in R-10 |
| 3 | Once stress reaches insolvency, recovery is poor — earliness has direct rupee value | 67% haircut on admitted claims; ₹3.99 lakh crore realised vs ₹12.31 lakh crore claimed (to Sep 2025) | IBBI data via [Business Standard](https://www.business-standard.com/industry/news/ibc-haircuts-2025-creditor-recovery-cirp-delays-ibbi-data-125112300278_1.html) | 2025 | §0/§6 headline value; G2's avoided-loss arithmetic |
| 4 | Wilful default has grown ~10× in a decade — borrower-behaviour signals, not just financials, matter | ₹3,83,264 crore outstanding, 18,318 wilful defaulters (Mar 2025) | Parliament/TransUnion CIBIL via [Moneylife](https://www.moneylife.in/article/from-39369-crore-to-383264-crore-in-10-years-indias-wilful-default-crisis-laid-bare-in-parliament-abg-shipyard-leads-at-6695-crore/79963.html) | 2025 | R-10 includes payment-behaviour and treasury-pattern evidence types |
| 5 | How banks cope today: borrower-submitted stock statements, QIS Forms I–III, MSOD returns, stock audits, IBA-empanelled Agencies for Specialised Monitoring for ₹250-crore+ exposures; RBI supervision found post-sanction monitoring weak (end-use lapses, reliance on CA certificates); EASE reforms push PSBs toward IT-based EWS | QIS applies to fund-based working-capital limits ≥ ₹5 crore; ASM threshold ₹250 crore (IBA framework, Oct 2018) | [CAclubindia on QIS/QMS](https://www.caclubindia.com/articles/beyond-loan-sanction-understanding-qms-in-working-capital-finance-and-credit-monitoring-55833.asp); [RBSA on ASM](https://rbsa.in/risk-consulting/strategic-risk-advisory/agency-for-specialized-monitoring/); [Business Standard on RBI findings](https://www.business-standard.com/article/finance/rbi-finds-lapses-in-monitoring-of-fund-diversion-111011500043_1.html); [PIB on EASE 8.0](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2237483&reg=3&lang=1) | 2018–2025 | The "before" state §22 measures against; R-09's certificate workflow; §6's hours-saved metric |
| 6 | A credible Indian incumbent exists — differentiation cannot be "we alert on borrower risk" | Crediwatch EWS: 200+ configurable alerts, 9–12-month lead claims, used by SBI, IndusInd, Central Bank of India | [Crediwatch EWS](https://about.crediwatch.com/about/product/early-warning-systems); [Azure Marketplace listing](https://marketplace.microsoft.com/en-us/product/saas/tchinformationanalyticsprivatelimited1654155358283.crediwatch_1?tab=overview) | 2025 | §4 differentiation; §22 evaluation questions |
| 7 | The global covenant-software field tracks dates or extracts documents; the sorting question in the trade is "does the tool test the covenant or only store the number a human entered" | Moody's, nCino (2,700+ FIs), Finastra LoanIQ + Eigen (100+ extracted fields), BankStride, Cync, Teslar | [Aloan category guide](https://aloan.ai/guides/best-covenant-monitoring-software); [Moody's loan monitoring](https://www.moodys.com/web/en/us/solutions/lending/loan-monitoring.html); [Finastra–Eigen](https://www.finastra.com/applications/eigen-technologies) | 2026 | §4's centre of gravity: Radar *tests* the covenant deterministically and dates the breach |
| 8 | Early-warning systems drown analysts in false positives; alert fatigue makes them ignore alerts — noise control is the unsolved half | ~95% false-positive rates reported in bank transaction monitoring; legacy EWS indicators described as producing overwhelming false alerts (worldwide figures — no India-specific rate found) | [Risk Hub](https://riskhub.org/blogs/early-warning-signals-of-credit-deterioration); [Flagright](https://www.flagright.com/post/understanding-false-positives-in-transaction-monitoring) | 2024–2025 | Makes persistence/materiality scoring (R-11) and §17's T3/T4 thresholds core, and makes G3 a first-class metric |
| 9 | SMA-2 stress among large borrowers is a documented lead indicator — rising SMA-2 translates into NPA slippage about two quarters later; large borrowers carried 54.6% of loans and 83.4% of GNPA at end-Sep 2018 | SMA-2 +58.6% Q1→Q2 FY19 preceded slippage | RBI FSR Dec 2018 via [Shankar IAS summary](https://www.shankariasparliament.com/current-affairs/financial-stability-report-rbi-1) | 2018 (dated; the mechanism, not the level, is what is used) | §3's causal argument; horizon design and §17's threshold rationale |
| 10 | No granted or pending patent specifically claiming ML-based loan-covenant breach-horizon prediction surfaced; adjacent art exists | Closest: [US6119103](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/6119103), [US20110078073A1](https://patents.google.com/patent/US20110078073A1/en); class [705/38](https://patents.justia.com/patents-by-us-classification/705/38) | USPTO / Google Patents / Justia | 2000–2011 | §4's originality claim is combination-novelty; a professional freedom-to-operate search is **[OPEN-01]**, required before commercial launch |
| 11 | Public Indian data exists to calibrate realism and to validate ratio ranges | RBI DBIE listed non-govt non-financial company aggregates; data.gov.in company master CSV/API; MCA V3 filings at ₹100/company/year | [RBI DBIE](https://data.rbi.org.in/); [data.gov.in MCA catalog](https://www.data.gov.in/ministrydepartment/ministry-corporate-affairs); [MCA guide](https://knowledgebase.bison.co.in/view_article.php?id=352) | 2025–2026 | R-02's reference-data loader and R-30's entity resolution; the demonstration portfolio's calibration |

### 2.1 The regulatory obligations this product must satisfy

In v0.1 several of these were noted and then deferred. In this release each is a requirement with a check.

| Instrument | What it requires | How this release satisfies it |
|---|---|---|
| **RBI Master Directions on Fraud Risk Management (commercial banks & AIFIs), effective 15 Jul 2024** — board-approved EWS and Red-Flagged-Account framework, EWS integration with core banking, Data Analytics Units, RFA reporting on CRILC within 7 days, fraud-or-not decision within 180 days ([SCC Online summary](https://www.scconline.com/blog/post/2024/07/17/rbi-revises-master-directions-on-fraud-risk-management-in-regulated-entities-legal-news/), [ELP note](https://elplaw.in/wp-content/uploads/2024/07/RBI-Revised-Master-Directions-on-Fraud-Risk-Management-July-2024.pdf)) | An auditable, reconstructable warning trail; EWS output that the bank's RFA process can consume; integration with the bank's core systems; human decisioning outside the tool | **R-20** audit trail and reconstruction · **R-29** core-banking/LOS connectors · **R-31** EWS/RFA pack and CRILC-format export · **P-01** advisory-only posture · **N-11** compliance evidence pack |
| **RBI Prudential Framework for Resolution of Stressed Assets, 7 Jun 2019** — SMA-0/1/2 by days overdue (1–30/31–60/61–90), CRILC monthly reporting plus weekly default reports for aggregate exposure ≥ ₹5 crore, 30-day review period on default ([RBI notification](https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=11580&Mode=0)) | Native SMA vocabulary; CRILC-shaped reporting; the 30/60/90 cadence | **R-08** SMA banding from account conduct · **R-31** CRILC export and weekly default report · **R-27** review-period reminders |
| **Digital Personal Data Protection Act, 2023** — corporate borrowers are not data principals, but guarantors, promoters, directors and signatories are; penalties to ₹250 crore per contravention ([Cyril Amarchand FIG paper](https://corporate.cyrilamarchandblogs.com/2023/12/fig-paper-no-28-data-law-series-2-implications-of-digital-personal-data-protection-act-2023-on-indian-banks/)) | Purpose limitation, access control, retention limits, erasure on request, breach notification, no unnecessary disclosure to processors | **N-04** field-level encryption, RBAC, masking before any outbound call · **R-20** consent/erasure event types · **N-11** retention schedule with automated enforcement and a documented erasure procedure |
| **RBI FREE-AI Committee report, 13 Aug 2025** — seven sutras including Explainability, Accountability, Fairness; board-approved AI policy; proportionate oversight ([Dvara summary](https://dvararesearch.com/summary-of-the-rbi-free-ai-committee-report/), [KPMG note](https://kpmg.com/in/en/insights/2025/08/rbi-free-ai-committee-report-on-framework-for-responsible-and-ethical-enablement-of-artificial-intelligence.html)) | Per-decision explainability, named accountability, model inventory and oversight | **R-21** why-panel over every stage · **N-12** model governance: registry, approval, drift monitoring, rollback · **§17** the model-may/may-never contract |
| **RBI Master Direction on Outsourcing of IT Services, 10 Apr 2023** — cloud/AI services are material IT outsourcing; the bank retains responsibility for confidentiality, needs audit rights, provider incident reporting so RBI is told within 6 hours, and an exit strategy ([RBI MD PDF](https://fidcindia.org.in/wp-content/uploads/2023/04/RBI-OUTSOURCING-OF-IT-SERVICES-10-04-23.pdf)) | Minimum necessary data to any processor; full call logging; a working exit path | **N-04** outbound whitelist and masking, fails closed · **N-02** per-call model log with cost accounting · **§13** pluggable provider layer so the bank can switch or self-host the model — the exit strategy is an architecture property, not a promise |
| **CERT-In Directions, 28 Apr 2022** — cyber incidents reported within 6 hours; ICT logs retained 180 days within India ([Trilegal note](https://trilegal.com/news-insights/how-to-comply-with-cert-ins-new-six-hour-time-frame-to-report-cyber-incidents/)) | Log retention in-country for 180 days; NTP sync; an incident runbook | **N-02** 180-day retention default with rotation and integrity hashing · **N-06** NTP requirement in the install checklist · **§20** incident runbook with the 6-hour clock |
| **Checked and inapplicable** | Aadhaar/eKYC limits (no retail identity verification); NPCI/UPI rules (no payments rail); GST rules (GST data enters only as an optional signal feed under R-30, never as a tax workflow); TRAI; SEBI; IRDAI | — | — |

**Three findings changed the design.** (1) GNPA at a 15-year low flipped the framing from cleanup to prevention, so the value anchor is the 67% haircut and ₹33,148 crore of loan frauds, not an NPA mountain. (2) Crediwatch plus the Moody's/nCino/Eigen field ruled out a generic alert dashboard and located the open ground at deterministic covenant *testing* with a dated horizon and per-stage audit. (3) The alert-fatigue literature promoted persistence/materiality control from a feature to the core of the mechanism — `problem.md`'s "meaningful deterioration vs temporary noise" is the industry's known unsolved half.

---

## §3 The mechanism, and why it works

### 3.1 In plain sentences

Covenant Radar keeps a live, machine-readable, version-controlled register of every covenant a borrower signed; recomputes each one on its contractual schedule and again whenever fresh data arrives; and scores everyday behavioural signals — payments, utilisation, cash flows, concentration, industry and news — for whether they are sustained deterioration or passing noise. It projects each covenant's headroom forward and tells the credit team, with evidence, which borrower is likely to break which covenant in 30, 60 or 90 days, what is driving it, what each candidate intervention would do to that date, and what to do first. Anyone can open "why" on any number and see the whole chain that produced it, including the stages no model touched.

### 3.2 The seven stages

Stages 2–6 are decided by code. Stage 1 is model-proposed, code-verified and human-confirmed. Stage 7 is the model writing prose over computed facts, checked in code before display.

| Stage | In | Out | Decided by |
|---|---|---|---|
| **1. Covenant digitisation** | Sanction letters, amendment letters and compliance certificates as PDF, scan or text | Structured covenant records: definition, threshold, direction, testing frequency, effective dates, exceptions, cure periods, source span — proposed by the model, re-verified by code (schema check, independent recomputation against the ratio library, range sanity, unit and currency check, effective-date consistency), confirmed by a credit officer. Invalid proposals are rejected, never silently accepted | Model proposes → **code verifies (fails closed)** → human confirms |
| **2. Covenant engine** | Confirmed registry + borrower financial periods + account conduct | Each covenant tested at its frequency and on data arrival: current value, threshold in force, headroom %, verdict, exception or waiver applied, cure-period state, SMA band | **Code** |
| **3. Evidence ledger** | Daily signal events (payments, utilisation, treasury flows, concentration, industry indicators, news) | Typed evidence items with persistence score, materiality score and decay state; each classified sustained vs transient against §17's T3/T4; contradicting evidence supersedes without deleting | **Code** |
| **4. Horizon forecast** | Stage-2 headroom series + stage-3 sustained evidence | Per covenant per horizon: breach probability, confidence, projected date the trajectory crosses the threshold, top drivers with contribution shares, and the full daily path | **Code** — transparent trend projection + signal-pressure adjustment + calibrated mapping; every term inspectable |
| **5. Intervention simulation** | A stage-4 forecast + a catalogue intervention with a parameterised effect | The counterfactual trajectory, the moved crossing date, the probability delta, and the assumption set the simulation rests on | **Code** |
| **6. Portfolio triage** | All stage-4 forecasts + exposure + case state | Borrowers/facilities ranked by urgency = probability × exposure × confidence, banded against §17's T1/T2, with what-changed since the prior run | **Code** |
| **7. Intervention memo** | Stages 2–6 outputs for one borrower + the role-tagged action catalogue | A warning memo: situation, covenant position, drivers, evidence citations, simulated options, recommended interventions for RM/credit/risk — every figure copied from records, never generated; shape-checked before display; exportable to PDF and DOCX | **Model** (grounded, checked by code) |

### 3.3 The reasoning, one step at a time

Because RBI's own data says behavioural stress (SMA-2) precedes NPA slippage by about two quarters (§2 row 9), signals that exist *between* reporting cycles carry usable early information. Because today's monitoring is quarterly, borrower-submitted and manual (§2 row 5), that information is currently discarded. Because covenant terms are contractual and computable, the breach condition itself needs no AI — computing it deterministically makes the warning auditable, which the 2024 Fraud Directions require. Because analysts ignore noisy alerting systems (§2 row 8), a persistence-and-materiality filter must sit between signals and escalation, or the product joins the ignored pile. Therefore: deterministic covenant testing + filtered behavioural evidence + horizon projection surfaces true deterioration weeks before a scheduled test date, without drowning the desk.

**What would falsify this.** If, on §17's evaluation set, the full mechanism finds deteriorating borrowers no earlier and no more precisely than the naive distance-to-threshold rule, the pipeline's extra stages are decoration. That head-to-head is not an argument; it is the harness's permanent second arm (N-01), it runs on every commit, and its result is printed in the release notes.

### 3.4 Where AI earns its place, and where it does not

AI earns **stage 1**, because covenant clauses are unbounded natural language and a rule parser handles only the formats it anticipated — §17's extraction set tests exactly this against a regex baseline. AI earns **stage 7**, because turning forty computed facts into a memo an RM acts on is language work. AI is deliberately **refused** stages 2–6: ratios, tests, persistence scoring, projection, simulation and ranking are arithmetic, where a model would add error and subtract auditability.

An optional statistical stage-4 challenger is permitted under **N-12**'s governance — registered, versioned, approved, monitored for drift, rollback-able, and answering the same why-panel contract as the deterministic model. It is off by default, and no deployment may enable it without a named risk owner's approval recorded in the audit trail. The deterministic forecaster remains the champion until a challenger beats it on the harness and a human approves the promotion.

### 3.5 Coverage of the problem statement

| `problem.md` requirement | Answered by | Carried by |
|---|---|---|
| Monitor covenant thresholds with financial/behavioural signals | Stages 2–3 | R-05, R-07, R-08, R-10, R-11 |
| Forecast breach risk at 30/60/90 days | Stage 4 | R-12 |
| Explain drivers of deterioration | Stage 4 + why-panel | R-13, R-21 |
| Recommend prioritized interventions | Stages 5–7 | R-14, R-15, R-16, R-17 |
| Distinguish deterioration from noise (core challenge) | Stage 3 persistence/materiality + T3/T4 | R-11, N-01 |
| Ingest financial statements, calculate ratios | Ingestion + engine | R-03, R-07 |
| Extract/represent covenant definitions, thresholds, frequency, exceptions | Stage 1 | R-04, R-05, R-06 |
| Monitor account activity, payments, utilization | Stage 3 + connectors | R-10, R-29 |
| Evaluate treasury flows and cash-pattern changes | Stage 3 treasury evidence type | R-10, R-11 |
| Concentration exposure + industry/news deterioration signals | Stage 3 evidence types over real and synthetic feeds | R-10, R-30 |
| Predict breach probability at 30/60/90 | Stage 4 | R-12 |
| Identify primary drivers | Stage 4 attribution | R-13 |
| Rank borrowers/facilities by urgency and impact | Stage 6 | R-14 |
| Recommend RM/credit/risk interventions (advisory, human review) | Stage 7 + role-tagged catalogue | R-16, R-17, P-01 |
| Auditable warning trail (data, trends, calculations, reasoning) | Every stage writes audit and trace events | R-20, R-21 |
| Covenant condition kept distinct from behavioural/external indicators | Registry and ledger are separate records, separate screens, separate stages | R-05, R-11 |
| Risk view revisable when later evidence changes interpretation | Evidence supersession + override capture | R-11, R-19 |
| Confidence and materiality visible | Stage 4 confidence + stage 3 materiality on every warning | R-11, R-12 |
| Executable locally, no live core-banking integration *required* | Runs entirely on one host; connectors are optional and configurable | §11, R-29 |

No requirement is unanswered, and none needed an exclusion to escape.

---

## §4 Competitive landscape and differentiation

**Products.**

- [Moody's Lending Suite / CreditLens](https://www.moodys.com/web/en/us/solutions/lending/loan-monitoring.html) — automates covenant *document* collection, validation and testing schedules. Radar differs by projecting a dated breach horizon forward and exposing per-stage reasoning, not compliance status alone.
- [nCino commercial lending](https://www.ncino.com/solutions/commercial-lending) — covenant due-date notifications and compliance records inside an origination platform. Radar tests covenants from data and forecasts, rather than reminding humans to.
- [Finastra LoanIQ + Eigen](https://www.finastra.com/applications/eigen-technologies) — extracts 100+ fields from compliance certificates. Radar's stage 1 differs by *re-verifying the extraction in code against a ratio library and failing closed* before anything is registered.
- [BankStride](https://www.bankstride.com/covenant-tracking-monitoring), [Cync](https://aloan.ai/guides/best-covenant-monitoring-software), [Teslar](https://aloan.ai/guides/best-covenant-monitoring-software), [Abrigo](https://aloan.ai/guides/best-commercial-lending-software) — tracking-layer tools: dates, documents and exception workflow, no forecast.
- [9fin](https://9fin.com/features/covenants) — AI covenant analysis with cited answers over debt documents for buy-side credit teams; a research tool over documents, not a monitoring pipeline over live borrower data.
- [Crediwatch EWS](https://about.crediwatch.com/about/product/early-warning-systems) — Indian incumbent, 200+ alerts on public/private data with 9–12-month-lead claims. Alert-centric, not covenant-registry-centric: it flags entity risk; Radar tests the signed contract and dates the breach. The two are complementary, and R-30's feed adapter framework can consume an EWS vendor's signals as one more evidence source.
- [Lyzr covenant-monitoring blueprint](https://www.lyzr.ai/blueprints/banking/loan-covenant-monitoring-agent/), [metricsIQ](https://www.metricsiq.ai/industries/financial-services/covenant-monitoring/), [Anaptyss CovenAce](https://www.anaptyss.com/blog/ai-powered-covenant-monitoring-financial-institutions/), [Uptiq](https://www.uptiq.ai/glossary/what-is-covenant-tracking) — 2024–26 AI covenant products and marketing; none found combines deterministic recompute, persistence-filtered evidence, a dated horizon, intervention simulation and per-stage explainability in one auditable trail.

**Regimes in use today.** RBI's [EWS/RFA framework](https://www.scconline.com/blog/post/2024/07/17/rbi-revises-master-directions-on-fraud-risk-management-in-regulated-entities-legal-news/) is the compliance regime Radar's audit trail feeds — Radar is a tool under it, never a substitute. [QIS/stock-statement/MSOD monitoring](https://www.caclubindia.com/articles/beyond-loan-sanction-understanding-qms-in-working-capital-finance-and-credit-monitoring-55833.asp) is the borrower-reported quarterly regime Radar computes between. [Agencies for Specialised Monitoring](https://rbsa.in/risk-consulting/strategic-risk-advisory/agency-for-specialized-monitoring/) are human outsourced monitoring above ₹250 crore; Radar is the software layer below and beside that threshold, and R-31 exports the pack an ASM would otherwise assemble by hand. [EASE-programme IT EWS in PSBs](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2237483&reg=3&lang=1) is a mandate with uneven adoption; Radar is a concrete shape for it.

**Research.** [Altman 1968 Z-score](https://en.wikipedia.org/wiki/Altman_Z-score) is the interpretable distress baseline and the ancestor of N-01's baseline arm; Radar differs by working at covenant level with contract-specific thresholds. [Barboza et al.](https://www.sciencedirect.com/science/article/abs/pii/S0957417417302415) show ML beats classic ratios on accuracy but loses traceability; Radar takes the opposite trade deliberately and puts ML behind N-12's governance rather than in the default path. [Demerjian–Owens](https://www.sciencedirect.com/science/article/abs/pii/S0165410115000725) measure violation probability from Dealscan-style data; Radar holds the actual contract definition and recomputes it. [CovenantAI, Saunders et al. 2025](https://sbfc.sydney.edu.au/2025/papers/SBFC2025_6B2_P261.pdf) detect violations from SEC filings text — detection after the fact, not a forward horizon. [CUAD](https://ar5iv.labs.arxiv.org/html/2103.06268) and [ContractEval 2025](https://arxiv.org/pdf/2508.03080) establish that clause extraction has known, non-trivial error rates — which is exactly why stage 1's code re-verification exists and why extraction alone is not the novelty claim. A [systematic review of EWS in finance](https://arxiv.org/pdf/2310.00490) surveys the field without describing a covenant-registry-plus-dated-horizon system.

**Patents.** No granted or pending patent specifically claiming covenant-breach-horizon prediction surfaced in a web-scale search. Closest adjacent art: [US6119103](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/6119103) and [US20110078073A1](https://patents.google.com/patent/US20110078073A1/en); relevant class [US 705/38](https://patents.justia.com/patents-by-us-classification/705/38). A professional freedom-to-operate search under CPC G06Q 40/03 is **[OPEN-01]** and is a **gate on commercial launch**, not a nice-to-have.

**The distinctive mechanism, stated carefully.** The *verified-registry dated-horizon evidence ledger*: (a) a code-verified covenant registry that re-implements and re-verifies the contract's own arithmetic rather than storing a number a human entered; (b) a persistence-and-materiality-scored evidence ledger that decays noise instead of amplifying it; (c) a projection that outputs a *date* a specific covenant crosses its specific threshold, with a counterfactual for each candidate intervention; and (d) per-stage auditability that opens the code stages, not only the model call. Each part is known art. The combination — and the fails-closed verification, which inverts "AI extracts, human hopefully reviews" into "AI proposes, code disproves" — is what the search did not find. The claim is combination novelty and Indian-regime fit; **[OPEN-01]** decides anything stronger.

---

## §5 The capability pillars

Six pillars. Each is a capability a customer buys, each is fully funded for 1.0, and none has a "narrower version" — the concept does not exist in this document.

**S1 — From sanction letter to live covenant, without trusting the extraction**
A credit officer uploads the sanction letter PDF, the amendment letter or the compliance certificate. The system finds the covenant clauses, the model proposes structured fields, and code independently re-verifies every one: schema, definition recomputable against the ratio library on this borrower's actual statements, threshold inside the plausible band for that ratio, units and currency, frequency in the allowed set, effective dates consistent with the facility. What code cannot reproduce is struck with the failing check named, and the confirm control does not exist for a failed proposal. Registered covenants are versioned and immutable once tested.
*Beats today:* covenant terms live in PDFs and spreadsheets; Eigen-style extraction exists but registers what the model says. Radar registers only what code can independently recompute.
*Requirements:* R-04, R-05, R-06, R-07.

**S2 — The 30/60/90 breach radar with a draggable horizon and a movable date**
For every borrower, which covenant breaks first, on what projected date, at what probability and confidence, driven by which signals — with a horizon control that walks time forward and watches headroom deplete, and an intervention simulator that shows what a ₹5-crore limit reduction or a monthly-reporting requirement does to that date.
*Beats today:* quarterly QIS review discovers breaches after test dates; incumbent tools track dates or raise undated alerts. Nothing found dates the breach, shows the trajectory, and lets the officer move it.
*Requirements:* R-07, R-08, R-10, R-11, R-12, R-13, R-14, R-15, R-23.

**S3 — Committee-ready memos and a reconstructable audit trail**
An intervention memo whose every figure click-resolves to the record that produced it, exportable to PDF and DOCX for a credit committee pack; an override logged with reasons that revises the risk view while keeping both states; and a reconstruction view that rebuilds why any warning was ever raised, from source data through calculation, evidence, thresholds in force, forecast and recommendation — the Fraud-Directions-shaped trail, exportable as an evidence bundle for an inspector.
*Requirements:* R-17, R-19, R-20, R-21, R-25.

**S4 — It runs the desk, not just the screen**
Overnight batch scoring of the whole portfolio so the queue is ready before anyone logs in; email and webhook digests per role; in-app notifications; SLA-tracked cases with assignment and status; saved views and search; bulk export.
*Beats today:* a monitoring tool nobody opens is a report. This is the difference between a dashboard and a system of record for the monitoring process.
*Requirements:* R-18, R-27, R-28, R-33, R-34.

**S5 — It plugs into the bank it is installed in**
Connector framework for core banking, loan origination and treasury: scheduled file drop (CSV/XLSX/fixed-width), REST pull, and database view read, each with per-field provenance, reconciliation reports and quarantine for rejected rows. External feed adapters for industry indicators, news and credit-bureau signals with entity resolution to CIN. Regulatory exports: CRILC-format, EWS/RFA pack, board MIS. A documented public REST API with OpenAPI so the bank's own systems can read Radar's outputs.
*Requirements:* R-29, R-30, R-31, R-32.

**S6 — It can be run, audited and defended by the bank's own people**
Bank SSO or a local user store, server-enforced role permissions, maker-checker on covenant registration and threshold change, field-level encryption of personal data, secrets outside the codebase, structured logs and metrics with SLOs, a documented backup and restore procedure with a tested recovery objective, versioned migrations, an installer, an upgrade path, a rollback path, and a compliance evidence pack.
*Requirements:* N-02, N-04, N-06, N-08, N-10, N-11, N-12, R-26.

---

## §6 Goals, business value and success metrics

| # | Goal | Who is better off, by how much | The number that says it happened | Baseline (§2) | Measured where | Result that would mean the idea is wrong | Label |
|---|---|---|---|---|---|---|---|
| **G1** | Warn before the breach, not after | Credit/risk teams gain ≥30 days of intervention room per deteriorating account | ≥70% of deteriorating borrowers flagged ≥30 days before their covenant test date crosses; ≥50% flagged ≥60 days before | 0 — today's discovery is at or after the quarterly test (§2 row 5) | N-01 harness on every commit; customer portfolio backtest in pilot | Radar flags no earlier than the naive headroom baseline arm | `MEASURED` on the harness; `PILOT` on a real book |
| **G2** | Convert earliness into recovery value | The bank. Anchor arithmetic: at the 67% average haircut (§2 row 3), a 1-percentage-point recovery improvement on one ₹50-crore exposure is ₹50 lakh | ₹ value = exposure × recovery-point gain, reported per intervention in the pilot ledger | ₹3.99 lakh crore realised vs ₹12.31 lakh crore claimed (§2 row 3) | Customer pilot, 2 quarters, against a matched control cohort | Early warning produces no measurable recovery or migration delta | `PILOT` |
| **G3** | Do not drown the desk | Risk officers; alert volume stays actionable | ≤10% of the monitored portfolio in amber-or-worse on any scoring day; false-escalation rate ≤5% on the labelled evaluation set; ≥60% of escalations acted on rather than dismissed | Legacy EWS false-positive rates reported near 95% (§2 row 8) | N-01 harness; in-product disposition telemetry (R-19) | Radar escalates the noise cohort — alert fatigue rebuilt | `MEASURED` + `PILOT` |
| **G4** | Usable by a credit officer without training | An RM or credit officer new to the tool completes F-01 unaided | ≤3 minutes, ≤2 stalls, zero assistance, on a moderated test with ≥5 participants from the customer's own desk | No comparable figure exists for the manual QIS route | Moderated usability sessions at M-5 and again in pilot | Participants cannot reach a memo without being rescued | `MEASURED` at M-5 |
| **G5** | Every shown figure defensible | Auditors and inspectors; trust in the trail | 100% of sampled on-screen figures (n≥50 per release) resolve to their source record via why-panel or audit trail; the evidence bundle for any warning exports in one action | RBI inspections found documentation reliance on borrower/CA certificates (§2 row 5) | §23 release sweep, sampled by the release manager | Any figure that resolves nowhere | `MEASURED` per release |
| **G6** | Monitoring effort per account falls | Credit administration; hours returned to the desk | ≥60% reduction in analyst-minutes per account per quarter versus the manual QIS baseline, measured by time-and-motion on the same accounts | Manual QIS/stock-statement review (§2 row 5) | Customer pilot time-and-motion study | No measurable reduction, or the tool adds work | `PILOT` |
| **G7** | It stays up and stays fast | Every user | 99.5% monthly availability during the customer's business hours; §18's latency targets met at p95; overnight batch completes before 07:00 IST on ≥99% of nights | — | Production telemetry (N-02), monthly SLO report | Repeated missed batches or latency regressions | `MEASURED` in production |

**Why these metrics and not the easy ones.** Alerts raised, borrowers scanned, dashboards viewed and model calls made would all look excellent while the product failed — a system that alerts on everything maximises them (§2 row 8). G1 and G3 are a deliberate pair: earliness *and* quiet, because either alone is trivially gamed by moving a threshold. G6 exists because a monitoring product that adds net work will be abandoned regardless of its accuracy.

---

## §7 Users, roles and stakeholders

Roles are **enforced server-side** (N-04). The table below is the authorization model, not an aspiration.

| Role | What they need | What they may do | What they must never do | Enforcement |
|---|---|---|---|---|
| **Relationship Manager** | An early, specific, evidence-backed heads-up on their accounts, days before the covenant test date | View their assigned portfolio and case files; read and export memos; record an action taken; add a comment; request a compliance certificate | Change covenant definitions, thresholds or forecasts; delete evidence; view borrowers outside their assignment unless granted | RBAC + row-level portfolio scoping |
| **Credit Officer** | Covenant intake without re-keying, and proposals they can trust because code re-verified them | Everything an RM may do, across their credit group; upload documents; review, correct and submit stage-1 proposals; register covenants; record waivers and amendments | Confirm a covenant that failed code verification (the control never renders and the endpoint refuses); approve their own registration where maker-checker is enabled | RBAC + fails-closed verification + maker-checker |
| **Credit Approver** | Assurance that what was registered matches the sanction letter | Approve or reject a pending covenant registration, amendment or waiver; compare the proposal against the highlighted source span | Register and approve the same record | Maker-checker: distinct-actor constraint enforced in the database |
| **Risk Officer** | A ranked, noise-controlled watchlist and the power to disagree | Everything above, portfolio-wide; generate memos; run intervention simulations; override a risk view with a reason; mark evidence disputed; manage watchlist status | Silently override — every override is captured with what was shown and why | RBAC + mandatory reason + audit event |
| **Risk Head / Approver** | Portfolio view, escalation control, model oversight | Everything a Risk Officer may do; approve threshold changes; approve model promotion under N-12; sign the monthly SLO and drift report | Edit or delete an audit event | RBAC + maker-checker on thresholds and model promotion |
| **Auditor / Inspector** | Reconstruction of any warning, and evidence they can take away | Read-only across everything, including the audit trail, model-call log, threshold history, connector reconciliation and configuration history; export an evidence bundle | Change anything, anywhere | Read-only role with no write grants at the database level |
| **Administrator** | A system that runs, users who can log in, connectors that reconcile | Manage users, roles and portfolio assignments; configure connectors, feeds and notification channels; edit thresholds (subject to approval); manage the action catalogue; run and monitor jobs; rotate secrets | Read borrower personal-class fields in the clear without an access-purpose record; edit the audit log | RBAC + break-glass access logging |
| **Data Steward** | Correct, reconciled, provenance-tracked data | Review quarantined rows, resolve entity-matching conflicts, approve reference-data updates, correct a mis-parsed statement with a reason | Alter a covenant test result directly | RBAC + corrections are new records, never edits |
| **Model gateway (external system)** | Well-formed requests within its limits | Receive masked, whitelisted prompts; return completions for stages 1 and 7 | Receive personal-class fields or raw identifiers; be trusted for any figure | N-04 whitelist, fails closed |

**Language and locale.** Corporate credit documentation is in English (**[ASSUMED-02]**), and English is the default UI language. Hindi is a shipped second language (R-35) because desk users at branch level are not all comfortable working in English, and the locale framework accepts further languages without a code change. Number formatting is Indian (₹, lakh, crore), dates are IST, and quarters are Indian financial-year quarters throughout.

---

## §8 Scope

### 8.1 In scope for 1.0

Everything below ships in the general-availability release. There is no phase-2 list in this document.

**Data and ingestion** — borrower, facility and portfolio master data with hierarchy and assignment · financial statement ingestion from CSV, XLSX, JSON and API with schema validation, unit and currency handling, and restatement support · document ingestion of sanction letters, amendment letters and compliance certificates as PDF (native and scanned, with OCR) with page-and-span provenance · account-conduct and transaction signal ingestion · connector framework for core banking, LOS and treasury (file drop, REST pull, database view) with per-field provenance, reconciliation and quarantine · external industry, news and bureau feed adapters with entity resolution to CIN · a seeded demonstration portfolio for evaluation, training and offline development.

**Covenant management** — versioned, effective-dated covenant registry with immutability once tested · a ratio library of 24 named definitions plus a validated custom-formula facility · exceptions, waivers, cure periods and grace periods as first-class, dated, audited objects · model-assisted, code-verified intake with maker-checker · covenant compliance certificate request, collection and tracking · covenant test scheduling by contractual frequency and on data arrival.

**Intelligence** — deterministic covenant engine with SMA banding · evidence ledger over six signal families with persistence, materiality, decay and supersession · 30/60/90 and arbitrary-horizon forecasting with probability, confidence, dated crossing and full daily path · driver attribution with contribution shares · intervention simulation against a parameterised action catalogue · portfolio triage and ranking with what-changed.

**Workflow and output** — case management with assignment, status, SLA and comments · grounded intervention memo with PDF and DOCX export · override, disposition and feedback capture · role-tagged, configurable action catalogue · notification and digest delivery by email, webhook and in-app · scheduled batch scoring and job orchestration with retry and alerting · regulatory and management exports including CRILC-format, EWS/RFA pack and board MIS · saved views, search, bulk operations and CSV export.

**Interface** — portfolio queue · borrower case file with the horizon control · covenant intake and review · intervention simulator · memo composer and export · audit and reconstruction · governance and model-oversight views · administration console · English and Hindi · light and dark themes · WCAG 2.2 AA.

**Platform** — authentication (local store with Argon2id, and OIDC/SAML for bank SSO) · server-enforced RBAC with row-level portfolio scoping · maker-checker · field-level encryption of personal-class data · append-only audit store with integrity hashing · structured logging, metrics, tracing, SLOs and alerting · versioned schema migrations · backup, restore and a tested recovery objective · installer, service registration, upgrade and rollback · public REST API with OpenAPI and scoped API keys · evaluation harness with a permanent baseline arm and regression gates · model governance registry with drift monitoring and rollback · documentation set: administrator guide, user guide, API reference, operations runbook and compliance evidence pack.

### 8.2 Out of scope — permanent product posture, not deferrals

These five are not schedule casualties. Each is a decision about what this product must never become, and each stays true in every future version.

- **P-01 — No automated credit decisions, waivers or escalations.** Every output is advisory and requires human review, per `problem.md` and the human-decisioning expectation in the 2024 Fraud Directions. The system may recommend, simulate, rank, notify and record. It may not approve, decline, waive, restructure or escalate on its own authority. No configuration flag exists to change this.
- **P-02 — No fraud classification.** Radar surfaces signals a Red-Flagged-Account process consumes and exports the pack (R-31); declaring an account fraudulent is a human, regulated act performed elsewhere.
- **P-03 — No origination, underwriting or sanction-time credit scoring.** Radar begins after sanction.
- **P-04 — No credit rating.** Radar produces covenant breach risk on a named contract, not an entity rating, and its outputs must not be presented as one.
- **P-05 — No autonomous action on borrower accounts.** Radar never places a hold, changes a limit, blocks a drawdown or initiates a payment. Connectors are read-only by design (R-29); the write path does not exist in the codebase.

### 8.3 Sequencing within 1.0

There is no cut list. Work is ordered by dependency in §28's milestones, and a milestone that runs long moves the release date rather than removing a capability. If commercial reality later forces a scope conversation, that conversation happens against §28's milestone boundaries with the customer, is recorded as a change to this document under §29, and is not pre-authorised here.

---

## §9 User flows

**F-01 — Morning triage (the primary flow).**
The risk officer signs in (SSO or local), lands on the portfolio queue already scored by the overnight batch and ranked by urgency with SMA bands and what-changed visible. They open the top borrower's case file → covenants with headroom and verdicts, the 30/60/90 forecast with confidence, the named drivers, the evidence ledger → move the horizon control and watch DSCR cross its threshold on a projected date → open the why-panel on the forecast stage and read the rule, the numbers, the thresholds compared and which side each value fell → open the intervention simulator, apply "reduce drawing limit by ₹5 crore" and see the crossing date move 41 days later → generate the memo, review it, export the PDF into the committee pack → assign the case to the RM with an SLA and log "RM call scheduled".
*Exception paths:* model provider unavailable at the memo step — the user is told plainly, everything computed locally still works, and the memo can be queued for retry · forecast confidence below T2 — the screen shows "insufficient evidence — watching" with the reason, never a bare probability · the officer disagrees — the override is captured with a mandatory reason, the view is revised, both states are retained and reconstructable.

**F-02 — Covenant intake from a document.**
The credit officer opens a facility → *Add covenants* → uploads the sanction letter PDF → the system extracts text (OCR if the page is a scan), locates candidate covenant clauses and shows each beside the highlighted source span → the model proposes structured fields for each → code re-verifies and marks every check pass or struck → the officer corrects the testing frequency on one, rejects a false positive, and submits → an approver compares each proposal against its source span and approves → the covenants go live and are tested immediately against the borrower's stored statements.
*Exception paths:* verification fails — the proposal is shown struck with the failing check named, the confirm control is absent, and hand entry is offered on the same screen · the document is unreadable or password-protected — the user is told which page failed and may upload a corrected file · text is empty, oversized or instruction-shaped — refused with a plain message and logged · the model provider is unavailable — the hand-entry form is the same screen with the proposal column absent · a duplicate covenant already exists — the officer is shown the existing record and asked whether this is an amendment.

**F-03 — Deterioration versus noise, and revision.**
Overnight, new signals land from the connectors: a delayed payment, a utilisation step-up, an unusual treasury outflow, an industry indicator. The evidence ledger scores each. A transient blip decays without escalating and stays visible on the ledger, greyed, with its decay state shown. A sustained pattern crosses T3, lifts the forecast, moves the borrower in the queue with a what-changed note, and fires the morning digest to the assigned RM and the risk desk. Two days later the payment arrives; the contradicting evidence supersedes, the risk view revises downward, the prior state remains reconstructable, and the queue movement is explained.
*Exception paths:* a signal contradicts the financials — both are shown, flagged "unreconciled", confidence is reduced, and a data-steward task is raised; they are never silently merged · a connector re-delivers a file — ingestion is idempotent on a content hash and nothing duplicates · a connector fails — the job alerts, the last good data stays in force, and the affected forecasts are marked with their data age.

**F-04 — Intervention simulation and the committee pack.**
The risk officer opens a borrower in the act band, runs three catalogue interventions side by side, sees the projected crossing date and probability for each against the do-nothing baseline, selects two, generates a memo that names them with their simulated effects and the assumptions each rests on, exports the PDF, and attaches it to a committee case. The simulation's assumptions are printed in the memo, because a counterfactual without its assumptions is a guess with a chart.

**F-05 — Audit and reconstruction.**
The auditor picks any past warning, by borrower, by date or by event id → the reconstruction view assembles source data with provenance, the covenant version in force, the calculation with its inputs, the trend, the evidence in force at that moment, the thresholds in force, the forecast with its formula terms, the memo and its model-call record, every override and every disposition → exports the whole thing as a signed evidence bundle (JSON + PDF + source-document copies) for the inspection file.
*Exception paths:* a warning whose source data has passed its retention date — the reconstruction shows what survives, names what was purged and under which retention rule, and never fabricates the gap.

**F-06 — Administration and configuration.**
The administrator adds a user and assigns a role and portfolio scope → configures a core-banking file-drop connector, maps its fields, runs it in dry-run mode, reviews the reconciliation report and the quarantine, then enables it on a schedule → edits a threshold, which enters pending state and requires the Risk Head's approval before taking effect → reviews the overnight job history and the drift report → rotates the model provider credential.

**F-07 — Compliance certificate cycle.**
The system raises certificate requests for covenants whose testing date is approaching, notifies the RM and (optionally) emails the borrower contact, tracks receipt, links the received certificate to the covenant test that consumes it, and escalates overdue certificates into the queue as an evidence item, because a missing certificate is itself a monitoring signal.

**F-08 — Onboarding a portfolio.**
The data steward imports borrowers, facilities and eight quarters of financials from the bank's extract, reviews the validation report and the quarantine, resolves entity-matching conflicts, imports or intakes the covenant register, runs a historical backfill so trends and forecasts exist from day one, and reviews the backfill's own reconciliation before the portfolio is released to users.

---

## §10 Requirements

Every requirement below is priority `must` for 1.0. Effort is in **ideal engineer-days** (one uninterrupted day of a competent engineer with an AI coding assistant, including that requirement's own tests). §28 sums them.

Mechanism coverage: stage 1 → R-04/R-05/R-06 · stage 2 → R-07/R-08 · stage 3 → R-10/R-11 · stage 4 → R-12/R-13 · stage 5 → R-15 · stage 6 → R-14 · stage 7 → R-16/R-17 · cross-stage audit → R-20/R-21.

### Data foundation

#### R-01 · Persistent data model, migrations and reference data
Flows: all · Effort: 4
**Why:** every later number is only as defensible as the record it came from; an unversioned schema makes a two-year-old warning unreconstructable.
**Does:** the full relational model of §14 in PostgreSQL, defined as typed ORM models with Alembic migrations, forward-only, each reversible or explicitly marked irreversible with a stated reason. Reference data (industry taxonomy, ratio definitions, action catalogue, holiday calendar, FY quarter calendar) loads from versioned seed files. A single command creates an empty, valid database; another upgrades an existing one; a third verifies that models and migrations agree.
**Checks:**
- R-01.a Given an empty PostgreSQL instance, when the create command runs, then every table, index, constraint and reference row exists and the schema version is recorded.
- R-01.b Given a database at any released schema version, when the upgrade runs, then it reaches head with no data loss, and a round-trip of representative data is byte-identical for all non-generated fields.
- R-01.c Given the ORM models, when the drift check runs, then no difference exists between models and migration head; a difference fails the build.
- R-01.d (failure) Given a migration that fails mid-way, when it is applied, then the transaction rolls back whole and the recorded schema version is unchanged.
- R-01.e Given SQLite selected as the development engine, when the same migrations run, then the schema is created and the domain test suite passes unchanged.

#### R-02 · Borrower, facility and portfolio master data
Flows: F-06, F-08 · Effort: 4
**Why:** monitoring is scoped by who owns the account; without portfolio structure, row-level access control and assignment have nothing to attach to.
**Does:** borrowers with CIN, industry classification, group structure and related-party links; facilities with type, sanctioned limit, outstanding, drawing power, security and pricing; portfolios, branches, credit groups and user assignment; borrower contacts; effective-dated changes so a limit increase is a new record, not an overwrite. Create, read, update and deactivate through the UI and the API, every change audited.
**Checks:**
- R-02.a Given a facility whose limit changes, when the change is saved, then a new effective-dated record exists, the prior record remains, and a covenant test dated before the change uses the prior limit.
- R-02.b Given a user scoped to one portfolio, when they list borrowers, then only that portfolio's borrowers are returned, enforced in the query and not in the template.
- R-02.c (failure) Given a borrower with live facilities, when deactivation is attempted, then it is refused, naming the facilities, and nothing changes.
- R-02.d Given two borrowers with the same CIN, when the second is created, then it is refused as a duplicate and the existing record is offered.

#### R-03 · Financial statement ingestion and normalisation
Flows: F-08, F-03 · Effort: 5
**Why:** ratios are only as good as the statement lines beneath them, and real bank extracts are messy, restated and inconsistently signed.
**Does:** import of financial periods from CSV, XLSX, JSON and the API into a normalised chart of statement lines, with a configurable mapping per source; validation of totals and balance-sheet identities; unit and currency normalisation to ₹ crore; sign-convention handling; restatement support so a corrected prior period supersedes without deleting; per-field provenance recording the source file, row and mapping version; a validation report and a quarantine for rejected rows.
**Checks:**
- R-03.a Given a well-formed extract, when it is imported, then every period is stored, the balance-sheet identity holds within the configured tolerance, and the report shows zero rejects.
- R-03.b Given a restated prior period, when it is imported, then the new version supersedes, the old remains, affected covenant tests are marked for recomputation, and the change is audited.
- R-03.c (failure) Given a file with a mis-typed value in one row, when it is imported, then that row is quarantined with the failing rule named, every other row loads, and nothing partial is left behind.
- R-03.d Given a file in a currency other than INR with a stated unit of thousands, when imported with the matching mapping, then stored values are in ₹ crore and the conversion is recorded in provenance.
- R-03.e Given the same file imported twice, when the second import runs, then no duplicate periods are created.

#### R-04 · Document ingestion with OCR and span provenance
Flows: F-02, F-05 · Effort: 6
**Why:** covenant terms live in sanction letters, and re-keying them by hand is today's error-prone path. An extraction with no pointer back to the page it came from cannot be audited.
**Does:** upload and storage of PDF, DOCX, image and text documents against a borrower or facility; text extraction with layout awareness for native PDFs and OCR for scans; page, line and character-span indexing so any extracted field points at the exact source span; document classification (sanction letter, amendment, compliance certificate, stock statement, other); virus scanning; encryption at rest; retention per §21; and a viewer that highlights the span behind any extracted field.
**Checks:**
- R-04.a Given a native-text sanction letter, when it is ingested, then text and spans are extracted, and a known clause's span resolves to the correct page and offsets.
- R-04.b Given a scanned sanction letter, when it is ingested, then OCR runs, a confidence score is stored per page, and pages below the configured confidence floor are flagged for human review rather than used silently.
- R-04.c (failure) Given a password-protected or corrupt PDF, when it is ingested, then the upload is rejected with the reason and the page number where extraction failed, and no partial document record survives.
- R-04.d (failure) Given a file that fails the virus scan, when it is uploaded, then it is quarantined, never written to the document store, and a security audit event is raised.
- R-04.e Given any extracted covenant field, when the user clicks it, then the viewer opens the source document at the highlighted span.

### Covenant management

#### R-05 · Versioned covenant registry
Flows: F-02, F-01 · Effort: 5
**Why:** the contract's terms are the product's ground truth; unversioned terms make every later number indefensible.
**Does:** covenants stored per facility with definition (from the ratio library or a validated custom formula), threshold, direction, testing frequency, effective-from and effective-to dates, applicable exceptions, cure and grace periods, source document and span, headroom-warning level, status and version. Versions are immutable once a test has run against them; an amendment creates a new version and the old remains, with historical tests still referencing the version they used. Waivers are dated, reasoned, approved records that attach to a covenant version without altering it.
**Checks:**
- R-05.a Given a registered covenant that has been tested, when its terms are amended, then a new version exists, the old version is intact, and past test results still reference the old version.
- R-05.b (failure) Given a covenant whose definition names a ratio outside the library and is not a valid custom formula, when registration is attempted, then it is refused with the unknown-definition error and nothing is written.
- R-05.c Given a waiver approved for two quarters, when covenants are tested inside and outside that window, then the waiver applies only inside it and every test record names the waiver used.
- R-05.d Given maker-checker enabled, when the same user attempts to both register and approve a covenant, then the approval is refused, naming the constraint.
- R-05.e Given a covenant with a 30-day cure period that fails a test, when the cure window is open, then the state is `breach-cure-open` with the cure end date, and it becomes `breach-confirmed` only when the window closes without a passing retest.

#### R-06 · Model-assisted, code-verified covenant intake
Flows: F-02 · Effort: 7
**Why:** covenant text is unbounded natural language, and a rule parser handles only the formats it anticipated; but a model's proposal is a hypothesis, not a record.
**Does:** candidate clause detection over an ingested document or pasted text; a model proposal of structured fields per clause; and six independent code verifications — schema validity, definition present in the ratio library or a parseable custom formula, definition actually recomputable against this borrower's stored statements, threshold within the plausible band for that ratio type, units and currency consistent, frequency and effective dates in the allowed sets and consistent with the facility. Any failure strikes the proposal and names the failing check; the confirm control does not render and the confirm endpoint refuses. A person confirms; an approver approves where maker-checker is on. Every proposal, verification verdict and human action is audited.
**Checks:**
- R-06.a Given each well-formed clause in the evaluation set, when proposed and verified, then extracted fields match the hand-labelled answer at ≥0.95 field-level precision and recall after verification, and confirmation registers a live covenant.
- R-06.b (failure) Given a clause with an implausible threshold, when verified, then the proposal is struck with the range check named, no confirm control renders, and a direct POST to the confirm endpoint returns a refusal naming the failed check.
- R-06.c (failure) Given pasted text containing instructions to the model, when processed, then the output is either a valid proposal about covenant fields or a refusal — never any other behaviour — and the attempt is logged as a security event.
- R-06.d Given a clause whose testing frequency is ambiguous, when verified, then ambiguity is flagged rather than guessed, and the officer is asked to choose.
- R-06.e Given the model provider is unavailable, when intake is opened, then the hand-entry form renders on the same screen and every verification still runs on the hand-entered values.

#### R-07 · Ratio library and custom formula facility
Flows: F-01, F-03 · Effort: 5
**Why:** the contractual condition must be computed exactly, consistently and identically everywhere it appears.
**Does:** 24 named ratio and condition definitions computed from stored statement lines and facility data — leverage, DSCR, interest coverage, fixed-charge coverage, current ratio, quick ratio, TOL/TNW, debt/EBITDA, net debt/EBITDA, EBITDA margin, TNW floor, minimum net worth, maximum capex, utilisation %, drawing-power headroom, receivable days, inventory days, payable days, cash conversion cycle, working-capital gap, promoter shareholding floor, dividend restriction, asset-cover ratio, minimum liquidity — each with a documented formula, its required statement lines, its plausible band and its unit. A validated custom-formula facility accepts an expression over the same named statement lines, parsed into a restricted AST with no function calls, no attribute access and no imports, and is subject to the same verification and testing as a library ratio. All arithmetic is code; no model participates.
**Checks:**
- R-07.a Given the hand-worked cases in the evaluation set, when the library computes them, then every value matches the hand-worked answer exactly.
- R-07.b Given a statement period missing a line a ratio needs, when it is computed, then a not-computable result names the missing line; no value is invented and no exception escapes.
- R-07.c (failure) Given a denominator of zero or a sign that makes a ratio meaningless, when it is computed, then the defined not-computable behaviour is returned with its reason, never a division error and never a silent skip.
- R-07.d (failure) Given a custom formula containing a function call or an attribute access, when it is parsed, then it is refused before evaluation and the disallowed construct is named.
- R-07.e Given a custom formula referencing an unknown statement line, when it is validated, then it is refused naming the line.

#### R-08 · Deterministic covenant engine
Flows: F-01, F-03 · Effort: 6
**Why:** this is the contractual condition, and it must be computed identically, on schedule, with its exceptions applied, and kept distinct from behavioural indicators.
**Does:** tests every live covenant at its contractual frequency and again whenever data it depends on changes; applies the threshold in force for the tested period including waivers and exceptions; computes signed headroom as a percentage of the threshold; assigns a verdict from {pass, warning, breach, breach-cure-open, stale, not-computable}; records the covenant version, period, threshold used, exception used, inputs and computed-at; derives the SMA band (0/1/2) from days-past-due in account conduct; and writes a stage-2 trace row for every test.
**Checks:**
- R-08.a Given the hand-worked engine cases in the evaluation set, when the engine runs, then every result matches exactly, including verdict and headroom.
- R-08.b Given a covenant with an exception in force, when tested inside and outside the window, then the exception applies only inside it and the test record names it.
- R-08.c (failure) Given a borrower with a missing quarter, when tested, then the covenant is marked stale naming the last period available, no value is invented, and downstream forecast confidence is reduced.
- R-08.d (failure) Given a period making a ratio undefined, when tested, then the defined not-computable verdict is recorded with its reason and the covenant is flagged for human review.
- R-08.e Given a value exactly at its threshold, when tested, then the boundary behaviour documented in §17 applies, is recorded, and is identical on every path.
- R-08.f Given a facility 65 days past due, when SMA banding runs, then the band is SMA-2 and the derivation is in the trace.

#### R-09 · Compliance certificate workflow
Flows: F-07 · Effort: 4
**Why:** RBI inspections found monitoring reliant on borrower and CA certificates that arrive late or not at all; a missing certificate is itself a monitoring signal, not an administrative gap.
**Does:** derives certificate requirements from the covenant register and its testing calendar; raises requests ahead of each due date; notifies the RM and optionally emails the borrower contact from a template; tracks status through requested, received, under review, accepted and rejected; links a received document to the covenant tests that consume it; and raises an overdue certificate as an evidence item that affects the risk view.
**Checks:**
- R-09.a Given a quarterly covenant with a due date, when the lead time elapses, then a request exists with the correct due date and the notification is queued.
- R-09.b Given a certificate received and accepted, when the covenant is next tested, then the test references the certificate document.
- R-09.c Given a certificate overdue past its grace period, when the ledger is scored, then an evidence item of type `certificate_overdue` exists with the correct age and it is visible on the case file.
- R-09.d (failure) Given an accepted certificate later found to be for the wrong period, when it is rejected with a reason, then the link is removed, affected tests are marked for recomputation, and both states remain in the trail.

### Intelligence

#### R-10 · Signal ingestion framework
Flows: F-03, F-08 · Effort: 6
**Why:** the information that exists between reporting cycles is the whole thesis; it has to arrive reliably, typed and idempotently, from more than one kind of source.
**Does:** ingestion of six signal families — payment behaviour, facility utilisation, treasury and cash-movement patterns, concentration exposure, industry indicators and news/external events — from connectors, feeds, file drops and the API. Each event is typed, dated, attributed to a borrower and facility, carries a magnitude and a payload, and is keyed by a content hash so redelivery is idempotent. Late-arriving events are accepted and marked late rather than dropped. A per-source ingestion report records counts, rejects and lag.
**Checks:**
- R-10.a Given a batch containing all six families, when ingested, then each event is stored with its type, and the ingestion report reconciles counts against the source.
- R-10.b Given the same batch delivered twice, when ingested, then no duplicate events exist and the report says so.
- R-10.c Given an event dated before the current processing watermark, when ingested, then it is stored, marked late, and the affected days are recomputed.
- R-10.d (failure) Given a batch with an unknown event type, when ingested, then that event is quarantined with the reason, the rest load, and the report names the quarantine.
- R-10.e Given a source that fails mid-batch, when the job runs, then nothing partial is committed and the job alerts.

#### R-11 · Evidence ledger with persistence, materiality, decay and supersession
Flows: F-03, F-01 · Effort: 7
**Why:** this is `problem.md`'s core challenge. Without it Radar rebuilds the alert fatigue §2 row 8 documents, and every other capability becomes noise amplification.
**Does:** converts signal events into typed evidence items scored for persistence (consecutive-day run and event count in a rolling window) and materiality (projected headroom erosion as a share of the threshold), with geometric decay from last observation; classifies each as transient, sustained or superseded against T3 and T4; keeps transient items visible and decaying but excluded from forecast pressure; and supersedes rather than deletes when contradicting evidence arrives, keeping both states reconstructable. Writes a stage-3 trace row naming the thresholds compared and which side each value fell.
**Checks:**
- R-11.a Given the noisy-transient cohort replayed over its full history, when scored, then no member crosses the amber band on any day.
- R-11.b Given the deteriorating cohort, when its payment delays persist past T3, then the evidence flips to sustained on the exact day the rule specifies, and the flip is in the audit trail.
- R-11.c Given contradicting later evidence, when ingested, then the risk view revises, the prior item is marked superseded rather than deleted, and both states reconstruct.
- R-11.d Given an item at exactly T3's boundary and one at exactly T4's, when scored, then §17's documented inclusive behaviour applies on both.
- R-11.e Given a transient item decayed below the visibility floor, when the ledger renders, then it is still listed with its decay state, never hidden and never deleted.

#### R-12 · Horizon forecast with dated crossing
Flows: F-01, F-03 · Effort: 7
**Why:** this is the product's promised answer — probability, confidence and a *date*, per covenant, per horizon.
**Does:** projects each covenant's metric trajectory from a trend over recent periods adjusted by sustained-evidence pressure; walks the projection day by day to the maximum horizon; finds the first day it crosses the threshold in the covenant's direction; maps distance, velocity and pressure to a breach probability per horizon through a documented, inspectable function; computes a confidence score from data completeness, evidence support and data freshness; stores the full daily path and every intermediate term so the UI reads rather than re-models and the why-panel can show the arithmetic. Pure code; every parameter lives in configuration, never in a branch.
**Checks:**
- R-12.a Given the deteriorating cohort at a scoring date, when forecast, then direction is deteriorating, the 60-day probability exceeds the act threshold, and the projected crossing date is within ±10 days of the labelled breach.
- R-12.b Given the stable cohort, when forecast, then all horizons sit below the amber threshold.
- R-12.c Given any forecast, when its why-panel opens, then inputs, formula terms, thresholds compared, sides and driver shares are all displayed and internally consistent.
- R-12.d (failure) Given confidence below T2, when the case file renders, then "insufficient evidence — watching" with its reason appears in place of a probability, on every surface including the API and the digest.
- R-12.e Given fewer than two usable periods, when forecast, then confidence is zero, probability is absent, and the reason is recorded rather than a number being produced.
- R-12.f Given a covenant already in breach, when forecast, then the crossing date is the current date and the probability reflects certainty of the current state, not a projection.

#### R-13 · Driver attribution
Flows: F-01 · Effort: 3
**Why:** "explains the drivers of deterioration" is a stated requirement, and a probability with no attribution cannot be argued with or acted on.
**Does:** attributes the risk delta across the trend term, each sustained evidence item and each data-quality factor; normalises contributions to sum to one; lists any contribution at or above T5 with its share and its evidence link; folds the remainder into a single `other` row; and records the attribution in the trace with the method named.
**Checks:**
- R-13.a Given any forecast, when attribution runs, then listed shares plus `other` sum to 1.0 within floating tolerance.
- R-13.b Given a contribution exactly at T5, when attributed, then it is listed rather than folded.
- R-13.c Given a driver traceable to an evidence item, when the user clicks it, then the evidence item opens.

#### R-14 · Portfolio triage and ranking
Flows: F-01 · Effort: 4
**Why:** the desk needs an ordered morning; ranking by urgency, confidence and exposure is a stated requirement.
**Does:** ranks borrowers and facilities by urgency = probability × exposure × confidence at the worst covenant-horizon; bands against T1; computes what-changed against the prior scoring run; applies a documented, total tie-break so ordering is deterministic; supports filtering by band, portfolio, industry, assignee, SMA band and case status; and supports saved views.
**Checks:**
- R-14.a Given a scored portfolio, when the queue renders, then ordering matches independently recomputed urgency values exactly.
- R-14.b (failure) Given two borrowers with identical urgency, when ranked, then the documented tie-break applies, the ordering is total, and the rule is shown in the why-panel.
- R-14.c Given a borrower with no forecast, when the queue renders, then the borrower still appears with the reason, never omitted.
- R-14.d Given a band change since the prior run, when the queue renders, then what-changed names the prior band and the new one.

#### R-15 · Intervention simulation
Flows: F-04, F-01 · Effort: 5
**Why:** a risk officer's real question is not "how bad is it" but "what should I do first, and what does it buy me". Ranking without counterfactuals leaves that unanswered.
**Does:** applies a catalogue intervention's parameterised effect to the stored trajectory and recomputes the projection, the crossing date, the probability and the drivers; supports comparing up to four interventions against the do-nothing baseline side by side; states every assumption the counterfactual rests on and carries those assumptions into the memo; and records each simulation as an auditable artefact rather than a transient screen state.
**Checks:**
- R-15.a Given a borrower and one intervention, when simulated, then the delta shown equals the independently recomputed forecast difference.
- R-15.b Given four interventions compared, when rendered, then each shows its own crossing date, probability and assumption list against the baseline.
- R-15.c (failure) Given an intervention whose effect cannot apply to this covenant type, when selected, then it is refused with the reason rather than silently producing no change.
- R-15.d Given a simulation referenced by a memo, when the memo is reopened later, then the simulation and its assumptions are still retrievable.

### Workflow and output

#### R-16 · Action catalogue and intervention recommendation
Flows: F-01, F-04 · Effort: 3
**Why:** recommendations must be bounded, role-correct and configurable by the bank, not invented per memo.
**Does:** a configurable catalogue of interventions, each with an id, a role tag (RM, credit, risk), display text, a parameterised effect model used by R-15, applicability rules by covenant type and band, and an approval requirement flag. Recommendation ranks applicable actions by simulated headroom gained per unit of effort and role. Administrators may add, edit and retire entries; every change is audited; retired entries remain resolvable by historical memos.
**Checks:**
- R-16.a Given a borrower in the act band, when recommendations are produced, then every recommended action exists in the catalogue, is applicable to that covenant type, and carries the right role tag.
- R-16.b Given a retired catalogue entry referenced by an old memo, when that memo is opened, then the entry still resolves and is shown as retired.
- R-16.c (failure) Given an attempt to add a catalogue entry with no effect model, when saved, then it is refused, because an action R-15 cannot simulate cannot be recommended.

#### R-17 · Grounded intervention memo with export
Flows: F-01, F-04 · Effort: 5
**Why:** the memo is the artefact that leaves the system and enters a committee pack; it is where a hallucinated figure would do real damage.
**Does:** assembles a memo over a fixed template — situation, covenant position, drivers, evidence citations, simulated options with assumptions, recommended interventions, advisory closing — with every figure injected from records into named slots and the model writing only the connecting prose, labelled as drafted. Output is shape-checked in code before display: every slot resolved, every action from the catalogue, length within T6, no numeric token outside a slot. A failing answer is retried once and then refused; a refused memo writes no record and never renders partially. Export to PDF and DOCX with the bank's letterhead configuration, page numbering, generation timestamp, generating user and a document integrity hash.
**Checks:**
- R-17.a Given any generated memo, when audited, then every number in it resolves to a record and every recommended action exists in the catalogue with the right role tag.
- R-17.b (failure) Given a forced malformed model response, when the shape check fails twice, then the user sees the refusal, no memo record exists, and nothing partial renders.
- R-17.c (failure) Given the model provider down, when a memo is requested, then a plain unavailable message appears, the request may be queued, and everything else on the screen still works.
- R-17.d Given a memo exported to PDF, when the file is opened, then it contains the same figures, the integrity hash, the timestamp and the generating user.
- R-17.e Given a numeric token in the prose that is not in any slot, when the shape check runs, then the memo is refused.

#### R-18 · Case management
Flows: F-01, F-03 · Effort: 4
**Why:** a warning that nobody owns is a warning nobody acts on; the audit question "what did the bank do about it" needs a record.
**Does:** a case per borrower-and-warning with status (open, in progress, monitoring, escalated, closed), assignee, due date and SLA derived from band, comments with mentions, linked memos, simulations, documents and actions taken, and a closure reason taxonomy. SLA breaches escalate into the queue and the digest. Case history is append-only.
**Checks:**
- R-18.a Given a borrower entering the act band, when the batch runs, then a case is created or updated with the SLA for that band and the assignee from the portfolio mapping.
- R-18.b Given an SLA passing its due date, when the batch runs, then the case escalates, appears in the queue with its overdue marker, and is included in the escalation digest.
- R-18.c Given a case closed with a reason, when reopened later, then the prior closure and its reason remain in history.
- R-18.d (failure) Given a case closure attempted with no reason, when submitted, then it is refused.

#### R-19 · Override, disposition and feedback capture
Flows: F-01, F-05 · Effort: 3
**Why:** what a user does with an answer is the only real signal this product will ever get, and it is the raw material of every future improvement.
**Does:** captures overrides (what was shown, what the user did instead, which stage produced it, a mandatory reason, the model and prompt versions and the thresholds in force), dispositions (acted, monitoring, dismissed with a taxonomy) and lightweight accept/reject feedback on any shown answer. Overrides revise the displayed view while keeping both states. All of it is queryable as a labelled dataset for N-12.
**Checks:**
- R-19.a Given an override, when submitted, then a record exists carrying all named fields plus the thresholds snapshot, and both states reconstruct.
- R-19.b (failure) Given an override with no reason, when submitted, then it is refused and nothing is written.
- R-19.c Given a month of dispositions, when the labelled dataset is exported, then every disposition appears with its features and its outcome label.

#### R-20 · Audit trail, reconstruction and evidence export
Flows: F-05, all · Effort: 5
**Why:** this is the compliance artefact the Fraud Directions require and the reason an inspector can trust anything else on the screen.
**Does:** an append-only event store — no UPDATE or DELETE statement against it exists anywhere in the codebase, and the database role the application uses holds no such grant — with a hash chain linking each event to its predecessor so tampering is detectable. Every stage run, configuration change, registration, approval, memo, override, disposition, login, permission change, export and data correction is an event carrying actor, IST timestamp, request id, subject, payload, model and prompt versions where relevant, and the thresholds snapshot in force. Reconstruction assembles any warning end to end; export produces a signed evidence bundle including source documents.
**Checks:**
- R-20.a Given any warning, when reconstructed, then source data with provenance, covenant version, calculation, trend, evidence in force, thresholds in force, forecast, memo, overrides and dispositions are all reachable from one view.
- R-20.b (failure) Given an attempt to edit or delete an audit event through any route, then no such route exists, the application's database role lacks the grant, and a direct-database tamper breaks the hash chain and is reported by the integrity check.
- R-20.c Given an evidence bundle export, when verified, then its manifest hash matches its contents and every referenced document is included.
- R-20.d Given data purged under the retention schedule, when a warning that referenced it is reconstructed, then the view names what was purged and under which rule, and fabricates nothing.

#### R-21 · Explainability — the why-panel over every stage
Flows: F-01, F-05 · Effort: 5
**Why:** explainability is regulated (RBI FREE-AI), audited, and the proof of the product's central claim. A panel that explains only the model call cannot explain stages 2–6, which is where the decisions actually happen.
**Does:** from any shown decision, opens the run behind it: each stage in order, what it received, what it produced, whether code or a model decided it, the version of the deciding rule or prompt, which thresholds were compared and which side each value fell, the confidence, and the source records — one structure for code stages, model stages and any future statistical stage. A stage that did not run says so. Available in the UI, in the API and in the exported bundle.
**Checks:**
- R-21.a Given a warning, when why is opened, then every stage is present, each opening to its inputs, outputs and decider, and every threshold named shows its value, the observed value and the comparison result.
- R-21.b (failure) Given a stage that did not run, when why is opened, then that stage says "not run" and fabricates nothing.
- R-21.c Given a model stage, when why is opened, then the masked prompt version, the returned text and the code verdict on it are all shown.
- R-21.d Given JavaScript disabled, when the why URL is opened directly, then it renders as a full page — explainability never depends on script.

### Interface

#### R-22 · Portfolio queue screen
Flows: F-01 · Effort: 3
**Does:** ranked rows with borrower, exposure, worst covenant, dated risk, band, SMA band, assignee, case status and what-changed; filters, saved views, search and bulk export; one click into a case file; all four state families designed (empty, loading, error, degraded).
**Checks:** R-22.a ordering matches R-14 exactly · R-22.b filters and saved views round-trip · R-22.c empty and degraded states render with a next step · R-22.d 10,000-row portfolio paginates within §18's budget.

#### R-23 · Borrower case file with horizon control and simulator
Flows: F-01, F-04 · Effort: 7
**Does:** one screen per borrower: header facts, covenant strip with headroom and verdicts, forecast panel with the horizon control that reads stored daily paths and never re-models, the intervention simulator, the evidence ledger, the document and certificate strip, the memo area, case status and actions. Every trajectory sits beside the figures it plots. The confidence render guard is enforced here. All four state families designed.
**Checks:** R-23.a moving the horizon updates covenant lines, probability, dated crossing and lit drivers coherently at every step within §18's interaction budget · R-23.b a borrower with no evidence and one covenant renders designed empty states with no blank panels and no console errors · R-23.c below-confidence forecasts never render a bare probability · R-23.d the screen is fully operable by keyboard and readable by a screen reader · R-23.e simulator results match R-15 exactly.

#### R-24 · Covenant intake and review screen
Flows: F-02 · Effort: 4
**Does:** document upload with progress and OCR status; source span on the left, proposed fields on the right, verification verdicts inline; strike-through with the failing check named; confirm rendered only when every check passes; approval queue for maker-checker; hand-entry on the same screen; bulk confirm for multi-clause documents.
**Checks:** R-24.a a clean clause renders all-pass verdicts and a confirm control · R-24.b a failed proposal renders struck with the check named and no confirm control anywhere in the DOM · R-24.c a direct POST to confirm on a failed proposal is refused · R-24.d provider-down renders hand entry with verification intact.

#### R-25 · Audit, governance and reconstruction screens
Flows: F-05, F-06 · Effort: 4
**Does:** warning reconstruction timeline; audit event search by actor, subject, type and date with export; threshold and configuration history with before/after; model-call log with cost and verdicts; evaluation scoreboard showing both arms against the pass marks per release; model registry and drift view; evidence bundle export.
**Checks:** R-25.a reconstruction reaches every part from one view · R-25.b threshold change shows before and after with actor and approver · R-25.c scoreboard shows both arms and the release it belongs to · R-25.d bundle export verifies.

#### R-26 · Administration console
Flows: F-06 · Effort: 5
**Does:** users, roles, portfolio scoping and session management; SSO configuration; threshold management with approval workflow; action catalogue management; connector and feed configuration with dry-run, field mapping, schedule, reconciliation report and quarantine review; notification channel and template management; job monitoring with history, retry and alerting; retention policy configuration; system health and version information.
**Checks:** R-26.a a threshold change enters pending and takes effect only on approval, with both recorded · R-26.b a connector dry-run reports what it would ingest and writes nothing · R-26.c a failed job is visible with its error and is retryable · R-26.d an administrator cannot grant themselves a role they do not hold without a second approver.

#### R-27 · Notifications, digests and escalations
Flows: F-03, F-01 · Effort: 4
**Does:** in-app notification centre; per-role, per-user configurable email digests (morning queue, band changes, SLA breaches, certificate due and overdue, job failures); webhook delivery with signed payloads, retry with backoff and a dead-letter queue; quiet hours and digest bundling so a hundred changes are one email, not a hundred; unsubscribe and preference management; delivery status visible to the sender.
**Checks:** R-27.a a band change produces exactly one digest entry for each subscribed recipient, bundled · R-27.b a webhook endpoint failing three times lands in the dead-letter queue and alerts, and nothing is lost · R-27.c quiet hours defer rather than drop · R-27.d no personal-class field appears in any notification body beyond what the recipient's role may already see.

#### R-28 · Scheduled batch scoring and job orchestration
Flows: F-01, F-03 · Effort: 5
**Does:** the overnight pipeline — ingest, test, score evidence, forecast, attribute, rank, update cases, dispatch digests — as ordered, individually retryable jobs with a run ledger, per-job timing, idempotent re-runs, a partial-failure policy that never leaves the portfolio half-scored, a manual trigger for a single borrower or the whole book, and a completion deadline with alerting when it is missed. In-process scheduling with a database-backed job store so a restart resumes rather than repeats.
**Checks:** R-28.a a full run over the reference portfolio completes within §18's window and every borrower's forecast is fresher than 24 hours · R-28.b a job failing mid-run leaves no partial state, alerts, and resumes correctly on retry · R-28.c the same run triggered twice produces identical results and no duplicate notifications · R-28.d a run that misses its deadline raises the configured alert.

### Integration and interoperability

#### R-29 · Core-banking, LOS and treasury connector framework
Flows: F-06, F-08 · Effort: 7
**Why:** the Fraud Directions expect EWS integrated with core banking, and a monitoring product that cannot read the bank's own data is a spreadsheet with extra steps.
**Does:** a connector abstraction with three shipped transports — scheduled file drop (CSV, XLSX, fixed-width, with PGP-decrypt support), REST pull with pagination and auth, and read-only database view — plus per-connector field mapping, schema validation, per-field provenance, watermarking for incremental pulls, reconciliation reports against source control totals, quarantine for rejected rows, dry-run mode and a documented interface for a customer-specific connector. **Read-only by construction:** no connector may write to a source system, and no write path exists in the codebase (P-05).
**Checks:** R-29.a a configured file-drop connector ingests, reconciles against control totals and reports rejects · R-29.b an incremental REST pull resumes from its watermark after failure with no gap and no duplication · R-29.c a source schema change is detected and the run is refused with the difference named, rather than loading wrong data · R-29.d a code search proves no connector performs a write against a source system · R-29.e credentials for every connector come from the secret store and appear in no log.

#### R-30 · External industry, news and bureau feed adapters
Flows: F-03 · Effort: 5
**Why:** `problem.md` names industry deterioration and news signals; entity resolution is the hard half and is what makes them usable rather than noisy.
**Does:** a feed adapter interface with shipped adapters for a licensed news API, an industry-indicator source and a bureau/registry source, plus a synthetic generator for offline development and evaluation; entity resolution from feed entities to borrower CIN with a confidence score, a manual review queue for ambiguous matches, and a negative-match memory so a rejected match is not re-proposed; relevance and sentiment classification; deduplication across sources; and per-feed cost and quota accounting.
**Checks:** R-30.a a feed item naming a monitored borrower resolves to the correct CIN and becomes an evidence item · R-30.b an ambiguous match enters the review queue rather than being asserted · R-30.c the same story from two sources deduplicates to one evidence item citing both · R-30.d a feed outage degrades that evidence family only and is visible on the health view · R-30.e the synthetic generator produces a deterministic stream for evaluation.

#### R-31 · Regulatory and management reporting
Flows: F-05, F-06 · Effort: 5
**Does:** CRILC-format export including the weekly default report layout; an EWS/RFA pack per borrower assembling the trail an RFA committee needs; a board and management MIS with portfolio distribution by band, SMA migration, early-warning lead time achieved, escalation and disposition statistics and model performance; scheduled generation and delivery; every report reproducible from stored data with the parameters and the generation record retained.
**Checks:** R-31.a a CRILC export validates against the published layout and reconciles to source records · R-31.b an RFA pack contains every element the framework names and exports as one bundle · R-31.c a report regenerated for a past date reproduces the original byte-for-byte for all non-timestamp content.

#### R-32 · Public REST API
Flows: F-06, integration · Effort: 4
**Does:** a versioned, documented REST API over portfolio, borrower, covenant, test, evidence, forecast, driver, simulation, memo, case and audit resources, with OpenAPI 3.1, scoped API keys, per-key rate limiting, cursor pagination, filtering, conditional requests, consistent error envelopes, and a deprecation policy. Read-heavy; the write surface is limited to ingestion, disposition, case update and simulation, and never to anything P-01 forbids.
**Checks:** R-32.a the OpenAPI document validates and matches the implementation, verified by contract tests · R-32.b an API key scoped to one portfolio cannot read another's data · R-32.c rate limits return the documented status and headers · R-32.d no endpoint exposes a personal-class field to a key whose scope excludes it.

#### R-33 · Search and saved views
Flows: F-01 · Effort: 3
**Does:** full-text and structured search across borrowers, facilities, covenants, documents, memos, cases and audit events, respecting the caller's row-level scope; saved, named, shareable views over queue filters; recent-item history.
**Checks:** R-33.a search results never include a record outside the caller's scope · R-33.b a saved view round-trips exactly · R-33.c search over the reference portfolio returns within §18's budget.

#### R-34 · Bulk operations and export
Flows: F-01, F-06 · Effort: 3
**Does:** multi-select on the queue for assignment, status change, watchlist and export; CSV and XLSX export of any list view with the caller's scope applied; asynchronous export for large sets with notification on completion; every export recorded as an audit event including the row count and the filter.
**Checks:** R-34.a a bulk assignment writes one audit event per affected case and one summary event · R-34.b an export of 50,000 rows completes asynchronously and notifies · R-34.c an export respects row-level scope.

#### R-35 · Multi-language interface
Flows: all · Effort: 3
**Does:** all user-facing strings externalised into catalogues with a translation workflow; English and Hindi shipped; per-user language preference and a per-deployment default; locale-aware number, currency, date and quarter formatting with Indian conventions in every locale; pluralisation; right-to-left readiness in the layout even though neither shipped language needs it; a check that fails the build on a missing or untranslated key in a shipped language.
**Checks:** R-35.a every user-facing string resolves from a catalogue; a literal string in a template fails the build · R-35.b switching to Hindi renders every screen with no untranslated key and no layout overflow · R-35.c numbers render in lakh/crore convention in both locales.

#### R-36 · Theming
Flows: all · Effort: 2
**Does:** light and dark themes from one token set, following the system preference by default with a persisted user override; both themes meeting §15's contrast floors; print styles for memos and reports; a high-contrast variant.
**Checks:** R-36.a both themes pass the automated contrast check on every token pair in use · R-36.b the preference persists across sessions and is respected server-side on first paint, with no flash of the wrong theme · R-36.c print output is legible in monochrome.

### Non-functional requirements

#### N-01 · Evaluation harness, baseline arm and regression gates
Effort: 6
**Why:** the one number that separates a working system from a confident one. A product that cannot show its own score against an honest alternative is asking to be believed.
**Does:** a versioned example set covering extraction, engine exactness, boundary behaviour, persistence and materiality, forecast dating, false-escalation, grounding, refusal and usefulness; a harness that runs the product arm and a documented baseline arm (naive headroom rule for forecasting, regex parser for extraction, ungrounded single prompt for the memo) over every example and prints both scores against pass marks; deterministic replay of recorded model responses so the suite runs offline and in CI; regression gates that fail the build when a score drops below its recorded floor; and per-release scoreboard retention.
**Checks:** N-01.a both arms score every example, printed with pass or fail per mark · N-01.b a malformed example is named and the run continues · N-01.c the suite runs offline against recorded responses with no network access · N-01.d a deliberate regression below a recorded floor fails the build.

#### N-02 · Observability, SLOs and alerting
Effort: 5
**Does:** structured JSON logs with a request id propagated across every layer, log levels, sampling and redaction; a separate model-call log carrying prompt version, provider, model version, tokens in and out, latency, computed cost, check verdict, retries and refusal reason; Prometheus metrics for request latency, error rate, job duration and outcome, queue depth, evidence and forecast volumes, model latency, cost and refusal rate, and connector lag; OpenTelemetry traces; health, readiness and version endpoints; SLO definitions with error budgets and alert rules; 180-day in-country log retention with rotation and integrity hashing; and a documented alert-to-runbook mapping.
**Checks:** N-02.a one request id retrieves the whole story across app log, model log, audit events and traces · N-02.b every model call records all named fields; cost is computed from configured pricing and is never silently absent · N-02.c an unwritable log directory never breaks a user-facing flow and surfaces on the health view · N-02.d every alert rule names a runbook section that exists.

#### N-03 · Configuration and threshold governance
Effort: 3
**Does:** typed, validated configuration from files and environment with clear precedence and no secret in any file; every decision threshold in a versioned configuration object rather than a branch, changeable without a code change, subject to approval, snapshotted on every change and referenced by every record that used it; a startup validation that refuses to start on invalid configuration with the offending key and line named; and a configuration-difference view between environments.
**Checks:** N-03.a a threshold change re-bands the queue on the next run and records before, after, actor and approver · N-03.b a malformed configuration refuses startup naming the key and line; a malformed hot reload leaves the last good values in force · N-03.c a static check proves no threshold literal exists in a branch anywhere in the source.

#### N-04 · Security
Effort: 8
**Does:** authentication by local store with Argon2id and a configurable password policy, or by OIDC or SAML against the bank's identity provider, with MFA support, session fixation protection, idle and absolute session timeouts, and account lockout; server-enforced RBAC with row-level portfolio scoping applied in the query layer; maker-checker on covenant registration, threshold change, catalogue change and model promotion; field-level encryption of personal-class columns with a key held outside the database and a documented rotation procedure; TLS required, with certificate verification never disabled in a released build; secrets from environment or OS keyring only, never a file in the tree, with a pre-commit and CI secret scan; CSRF protection, security headers, a strict content security policy with no external origin, output escaping everywhere and parameterised queries only; upload validation, virus scanning and content-type verification; input validation at every boundary; outbound whitelisting and masking to the model provider that fails closed; rate limiting on authentication and on the API; dependency and container vulnerability scanning in CI; and a penetration-test remediation gate before release.
**Checks:** N-04.a an outbound capture across a full workload shows zero personal-class fields and zero key material · N-04.b a user without a permission receives the documented refusal from the endpoint, not merely a hidden control, proven per role per endpoint · N-04.c a scoped user cannot read another portfolio's data through the UI, the API or search · N-04.d a released build cannot disable TLS verification, proven by a test · N-04.e the secret scan passes on the whole history · N-04.f personal-class columns are unreadable in a raw database dump without the key · N-04.g all high and critical findings from the penetration test are closed or accepted in writing by a named owner.

#### N-05 · Performance and capacity
Effort: 3
**Does:** meets §18's targets at the stated portfolio sizes, with load tests in CI at the reference size and a documented capacity model relating portfolio size to batch duration, database size and memory.
**Checks:** N-05.a every §18 row is measured on the reference hardware and printed per release · N-05.b the load test at the reference size passes within budget · N-05.c a 10× portfolio is measured and its numbers are published even where they miss.

#### N-06 · Reliability, backup and recovery
Effort: 4
**Does:** documented and *tested* backup of database, documents and configuration; a restore procedure with a measured recovery time and recovery point objective; graceful shutdown that finishes in-flight work; startup self-checks; idempotent jobs; database connection pooling with retry and circuit breaking; and a data-integrity check that verifies the audit hash chain and referential integrity on a schedule.
**Checks:** N-06.a a restore from backup into an empty host reproduces the system and meets the stated objectives, rehearsed and timed · N-06.b a hard kill during the overnight batch leaves no partial state and the retry completes correctly · N-06.c the integrity check detects a deliberately corrupted audit chain.

#### N-07 · Accessibility
Effort: 3
**Does:** WCAG 2.2 Level AA across every screen: contrast floors, full keyboard operability including the horizon control, visible focus, semantic headings and landmarks, table semantics, labelled controls, text equivalents for every figure, reduced-motion support, and a 200% zoom and 320px reflow that stays usable. Verified by automated checks in CI and by a manual audit with a screen reader before release.
**Checks:** N-07.a the automated audit reports zero violations on every screen in both themes and both languages · N-07.b the primary flow completes by keyboard alone · N-07.c a screen-reader walkthrough of the primary flow is recorded and its findings are closed.

#### N-08 · Deployability, upgrade and rollback
Effort: 5
**Does:** an installer for the supported single-host targets that provisions the runtime, database, service registration, log directories and TLS configuration and runs the post-install verification; an upgrade command that backs up, migrates, restarts and verifies, with automatic rollback on failure; a documented manual rollback; version and build metadata surfaced in the UI, the API and the logs; and a reproducible build with a pinned dependency lock and a generated software bill of materials.
**Checks:** N-08.a a clean install on a fresh host reaches a working system within the documented time, performed by someone who did not write the installer · N-08.b an upgrade from the prior release preserves all data and passes verification · N-08.c a deliberately failing upgrade rolls back automatically and the system is left working on the prior version · N-08.d the build is reproducible and the bill of materials is generated.

#### N-09 · Test strategy and quality gates
Effort: 6
**Does:** unit tests for every domain rule; property-based tests for the ratio library, the ledger's scoring and the forecast's monotonicity invariants; integration tests against a real PostgreSQL instance; end-to-end browser tests for the primary flows in both themes; contract tests for the API against its OpenAPI document; migration tests including upgrade from the prior release; security tests for authorization at every endpoint and role; performance tests at the reference size; mutation testing on the domain core; and CI gates on coverage, lint, type-check, security scan, accessibility, evaluation scores and performance.
**Checks:** N-09.a domain-core line coverage ≥90% and branch coverage ≥85%, enforced in CI · N-09.b every endpoint has an authorization test for every role · N-09.c the full suite runs offline with no network access · N-09.d mutation score on the domain core is at or above its recorded floor.

#### N-10 · Documentation
Effort: 4
**Does:** an administrator guide (install, configure, connectors, users, thresholds, backup, upgrade, troubleshoot); a user guide per role with the primary flows; an API reference generated from the OpenAPI document with examples; an operations runbook with one section per alert; an architecture decision record set; a model card for each model-using stage; and release notes carrying the evaluation scoreboard.
**Checks:** N-10.a a new administrator installs and configures the system from the guide alone, observed · N-10.b every alert rule in N-02 has a runbook section · N-10.c the API reference matches the implementation, verified by the contract tests.

#### N-11 · Compliance evidence and data governance
Effort: 4
**Does:** a data inventory classifying every field; a retention schedule per class with automated enforcement and a purge log; a documented erasure procedure for a data-principal request that preserves the audit trail's integrity while removing personal content; access-purpose logging for privileged reads of personal-class data; a compliance evidence pack mapping each regulatory obligation in §2.1 to the control, the requirement and the test that proves it; and an annual review record.
**Checks:** N-11.a every field in the model appears in the inventory with a class and a retention rule · N-11.b the retention job purges exactly what is due, logs it, and leaves the audit chain valid · N-11.c an erasure request removes personal content while every affected audit event remains chain-valid and is marked redacted · N-11.d the evidence pack's every row points at a test that exists and passes.

#### N-12 · Model governance
Effort: 4
**Does:** a registry of every model-using component with its provider, model identifier, prompt version, purpose, owner, evaluation record and approval; version pinning with a controlled promotion path; a champion/challenger facility for the statistical stage-4 alternative with promotion gated on the harness and a named approver; drift monitoring on input distribution, override rate, refusal rate and score, with alerts; automated rollback to the prior approved version on a breached guardrail; and a per-component model card.
**Checks:** N-12.a every model-using component appears in the registry with an approval record · N-12.b a prompt change without a version bump fails the build · N-12.c a simulated drift breach alerts and the documented rollback restores the prior version · N-12.d a challenger cannot be promoted without a harness result and a named approver, enforced in code.

---

## §11 Constraints and operating environment

### 11.1 What the deployment must fit inside

**The target is a single hardened host the customer owns and operates.** No container runtime is required. No component of the mechanism depends on an internet connection. The only optional outbound hop is the model provider, and the product is fully functional with it disabled — stages 1 and 7 degrade to hand entry and a template memo, and stages 2–6, which are the mechanism, are unaffected.

| Constraint | Statement | Consequence in this document |
|---|---|---|
| **Single host** | Application, database, scheduler and document store run on one machine — Windows Server 2022+ or Linux (RHEL 9 / Ubuntu 22.04+) — sized per §18's capacity model | §13 uses in-process scheduling and a local document store; no message broker, no distributed cache, no service mesh |
| **No container requirement** | Installation is a native service (Windows Service or systemd unit) with a per-application virtual environment. Containers are supported but never required | N-08's installer targets both; §25 documents both paths |
| **Air-gap capable** | The mechanism, the UI, the batch and the audit trail work with no outbound network. Model-using stages degrade gracefully and say so | R-06.e, R-17.c; N-01.c runs the whole evaluation suite offline |
| **Bank-controlled identity** | Authentication delegates to the bank's identity provider where one exists; a local store exists for deployments without one | N-04 |
| **Bank-controlled data** | All borrower data stays on the host. Only masked, whitelisted, non-personal derived values may reach a model provider, and only when that provider is enabled | N-04, §16 |
| **No external CDN** | Every font, script, stylesheet and asset is served from the host. Nothing loads from a third-party origin, enforced by the content security policy | §15, N-04 |
| **Supported database** | PostgreSQL 17 is the production database. SQLite is supported for development, tests and the offline evaluation harness through the same data-access layer | R-01.e, §13 |
| **Regulated retention** | Logs retained 180 days in-country; business records per the retention schedule; audit events retained for the regulatory period | N-02, N-11, §21 |

### 11.2 What the build must fit inside

| Constraint | Statement |
|---|---|
| **Execution model** | Exactly one coding agent works at a time — Claude Code or Codex, never both concurrently. The plan and task documents are written for a single serial worker with a human reviewer, and contain no lane fences, no parallel batches and no cross-agent coordination |
| **Development environment** | May be a locked-down corporate laptop: no administrative rights, packages from an approved registry, an outbound proxy. Everything in §13 installs per-user; the development database is SQLite where PostgreSQL cannot be installed, and the CI environment provides PostgreSQL for the integration suite |
| **Dependency policy** | Every dependency is pinned to an exact version in a lock file, vetted for licence compatibility, scanned for vulnerabilities in CI, and justified in §13. A dependency with no maintenance activity in twelve months requires a documented decision |
| **Model provider** | The provider layer is pluggable. The default deployment targets the TCS GenAI Lab gateway; adapters for other providers exist so a customer's own approved model can be used without a code change. The gateway's commercial envelope is **[OPEN-02]** |
| **Reproducibility** | Any released version can be rebuilt from its tag with an identical dependency set and a generated bill of materials (N-08) |

### 11.3 Assumptions

Each is registered in §12 with its consequence if wrong.

---

## §12 Dependencies, assumptions and critical path

### 12.1 External dependencies

| Dependency | Owner | If unavailable or late | On the critical path? |
|---|---|---|---|
| PostgreSQL 17 on the target host | Customer infrastructure | Development and evaluation proceed on SQLite; production install blocks until provisioned | **Yes**, for production install only |
| Model provider access (gateway or alternative) with a commercial envelope — rate limits, quota, context window, latency, token accounting and price: **[OPEN-02]** | Customer / provider | Stages 1 and 7 run in degraded mode: hand entry and template memo. Stages 2–6 are unaffected. Evaluation runs against recorded responses | **No** — the mechanism is never hostage to it |
| Bank identity provider metadata for SSO: **[OPEN-03]** | Customer IT | The local user store is used; SSO is configured later without a code change | No |
| Core-banking / LOS extract specifications and a sample: **[OPEN-04]** | Customer IT | The file-drop connector is built against the documented generic layouts and mapped at deployment | **Yes**, for R-29's customer-specific mapping |
| Licensed news / industry feed subscription: **[OPEN-05]** | Customer procurement | The synthetic generator supplies that evidence family; the adapter is built and tested against a recorded fixture | No |
| Customer portfolio history for backtesting and calibration: **[OPEN-06]** | Customer | Thresholds ship at their documented defaults with the calibration procedure documented; G1/G3 remain harness-measured until pilot | **Yes**, for G1/G2/G6's pilot measurement |
| SMTP relay and webhook endpoints for notifications: **[OPEN-07]** | Customer IT | Notifications queue and surface in-app; delivery is configured at deployment | No |
| Freedom-to-operate patent search: **[OPEN-01]** | Legal | Blocks commercial launch, not engineering | **Yes**, for launch |
| Penetration test slot: **[OPEN-08]** | Security vendor | Blocks release sign-off (N-04.g) | **Yes**, for release |

### 12.2 Assumptions register

- **[ASSUMED-01]** One coding agent plus one human reviewer, working serially. Wrong → §28's calendar changes; the ordering does not.
- **[ASSUMED-02]** Corporate-credit documentation and covenant text are in English. Wrong → R-35's catalogue framework already handles the UI; document extraction would need a language model configured for the document language, which the provider layer permits.
- **[ASSUMED-03]** The customer's exposures of interest are ≥ ₹5 crore, the CRILC band, and their portfolio is in the tens of thousands of facilities or fewer. Wrong → §18's capacity model is re-run and the host is resized; nothing structural changes below 100,000 facilities.
- **[ASSUMED-04]** Financial statements arrive at least quarterly and account conduct at least daily. Wrong → forecast confidence falls, which the system already reports rather than hides.
- **[ASSUMED-05]** The customer accepts advisory-only outputs with human decisioning (P-01). This is not negotiable and is stated in the contract.
- **[ASSUMED-06]** The host has NTP synchronisation and a correct IST clock. Wrong → CERT-In log correlation and SLA computation break; the install checklist verifies it.
- **[ASSUMED-07]** Documents are ingestible: sanction letters are PDF or scanned PDF, not photographs of screens. Wrong → OCR confidence falls below the floor and pages route to human review, which R-04.b already specifies.
- **[ASSUMED-08]** The customer will supply a test cohort of accounts with known outcomes for pilot calibration. Wrong → calibration uses the shipped defaults and G1 stays harness-measured.

### 12.3 The critical path

The dependency spine, longest first:

`R-01 data model → R-02 master data → R-03 statement ingestion → R-07 ratio library → R-05 registry → R-08 engine → R-10 signal ingestion → R-11 evidence ledger → R-12 forecast → R-13 attribution → R-14 triage → R-23 case file → R-15 simulation → R-17 memo → R-20 audit → R-25 audit screens → N-01 harness gates → N-08 install → release`

Everything else attaches to this spine and can be built alongside it in any order a single agent chooses, subject to §28's milestones. Three things are on the path and are *not* engineering: the customer's extract specification (**[OPEN-04]**), the portfolio history for calibration (**[OPEN-06]**), and the penetration test (**[OPEN-08]**). Each is requested at M-0 rather than discovered at M-5.

---

## §13 Architecture

### 13.1 Chosen stack

Every choice below is justified, pinned and has a documented fallback. Versions are those current at 2026-08-30.

| Layer | Technology | Why this one | Fallback |
|---|---|---|---|
| **Language / runtime** | Python 3.12+ (3.13 supported) | The domain is numeric and rule-heavy; the ecosystem carries the document, numeric and ML libraries this product needs; the team's stated competence | None — this is a foundational choice |
| **Web framework** | FastAPI 0.141.1 on Uvicorn 0.52.4 | Typed request and response models with automatic OpenAPI (R-32), dependency injection that makes RBAC enforcement a declarative concern rather than a per-route habit, async where I/O warrants it, and a large production base | Starlette directly; the routing layer is thin by design |
| **Validation / settings** | Pydantic 2.x, pydantic-settings | One validation model serving the API schema, the configuration loader and the domain DTOs; validation at every boundary is N-04's requirement and this makes it structural | dataclasses + explicit validators |
| **Data access** | SQLAlchemy 2.0 (typed ORM + Core) | Row-level scoping applied in the query layer rather than the template (R-02.b, N-04.c) needs a real query abstraction; supports both PostgreSQL and SQLite from one model set (R-01.e) | None — hand-written SQL would put scoping back in the callers |
| **Migrations** | Alembic 1.14+ | Versioned, forward-only, reversible migrations with a model-drift check (R-01.c) | None |
| **Database** | PostgreSQL 17 (production), SQLite 3.45+ (development, tests, offline harness) | Concurrent writers, real constraints, window functions for trend and ranking, JSONB for payloads and provenance, partial and expression indexes, and row-level security available as a defence in depth | None for production |
| **Templating / UI** | Jinja2 3.1.6, HTMX 2.x (vendored), hand-written CSS, vanilla JS modules, inline SVG rendered by the application | §15's design system is a document idiom, not a component-library idiom; server rendering keeps the authorization decision on the server; HTMX gives the interaction quality the horizon control needs without a build chain, an SPA state layer or a CDN | Full page loads; every screen is required to work without JavaScript except the horizon control, which degrades to fixed stops |
| **Scheduling** | APScheduler 3.11 with a SQLAlchemy job store | Single host, no broker; a database-backed store means a restart resumes rather than repeats (R-28) | A platform scheduler (Task Scheduler / cron) invoking the CLI; the job logic is in the domain, not the scheduler |
| **Documents** | pypdf + pdfplumber for native text and layout; OCRmyPDF/Tesseract for scans; python-docx for DOCX; WeasyPrint for PDF output | Native-text extraction with coordinates is what makes span provenance possible (R-04.a); OCR only where needed keeps cost and error down | Text-only extraction with a warning; the confidence floor already routes bad pages to humans |
| **Numeric** | NumPy + pandas 3.0.5 | Trend fitting, rolling windows and the daily path walk | Pure-Python statistics for the small paths; the algorithms are simple by design |
| **Model access** | A provider protocol with adapters: TCS GenAI Lab gateway (default, OpenAI-compatible), Azure OpenAI, Anthropic, and a recorded-response adapter for tests. httpx 0.28.1 + tenacity for transport and retry | The Outsourcing Direction requires an exit strategy; a provider interface *is* the exit strategy. The recorded adapter is what makes N-01.c's offline suite possible | Any OpenAI-compatible endpoint through the default adapter |
| **Security** | argon2-cffi (password hashing), authlib (OIDC), pysaml2 (SAML), cryptography (field-level encryption), itsdangerous (signed cookies) | Each is the maintained standard for its job; none is written in-house | None — cryptography is never hand-rolled |
| **Observability** | structlog, prometheus-client, opentelemetry-sdk | Structured logs, metrics and traces from one instrumentation point | Standard-library logging with a JSON formatter |
| **Quality** | pytest, pytest-cov, hypothesis, mutmut, ruff, mypy (strict on the domain), bandit, pip-audit, playwright (e2e), axe-core (accessibility) | N-09's gates need each of these; all run offline in CI | — |
| **Build / packaging** | pyproject.toml with a hatchling backend, uv or pip-tools for the lock, cyclonedx for the bill of materials | Reproducible builds are N-08's requirement | — |

### 13.2 Layers, and what each may know

```
┌──────────────────────────────────────────────────────────────────────┐
│ Presentation      web/ (Jinja templates, HTMX, CSS, JS)  api/ (REST) │
│   knows: application services, DTOs, the current principal           │
│   may never: compute a ratio, decide a band, originate a probability │
├──────────────────────────────────────────────────────────────────────┤
│ Application       services/ — use cases, transactions, authorization │
│   knows: domain, repositories, ports                                 │
│   may never: contain a business rule that belongs in domain/         │
├──────────────────────────────────────────────────────────────────────┤
│ Domain            domain/ — ratios, covenants, signals, forecast,    │
│                   triage, interventions. Pure functions over typed   │
│                   records plus configuration.                        │
│   knows: nothing above it, no framework, no I/O, no provider         │
│   may never: import web, api, ai, or any database session            │
├──────────────────────────────────────────────────────────────────────┤
│ Ports             protocols: LLMProvider, DocumentStore, Notifier,   │
│                   FeedAdapter, ConnectorTransport, Clock, Repository │
├──────────────────────────────────────────────────────────────────────┤
│ Adapters          db/ ai/ ingestion/ notifications/ documents/       │
│   knows: one external thing each, behind its port                    │
├──────────────────────────────────────────────────────────────────────┤
│ Cross-cutting     config/ security/ audit/ observability/            │
└──────────────────────────────────────────────────────────────────────┘
```

The rule that matters: **`domain/` imports nothing from any layer above it, and no framework at all.** An import-linter contract enforces it in CI. A domain module that knows about the model provider, the web request or the database session has stopped being auditable, and that is the whole product.

### 13.3 One request, walked

A risk officer opens a case file. The browser sends `GET /borrowers/B-07` over TLS to Uvicorn. Middleware mints a request id, resolves the session to a principal, and starts a trace span. The route's dependencies resolve the principal's permissions and portfolio scope and refuse before the handler runs if either fails. The application service loads the borrower, its facilities, the live covenant versions, the latest tests, the evidence ledger, the stored forecasts and their drivers, the case and the documents — every query carrying the scope predicate. Nothing is computed here: every figure already exists as a record, because the overnight batch computed it. Jinja renders the case file with inline SVG trajectories drawn from the stored daily paths. Middleware writes one structured log line and one metric observation carrying the request id.

The officer generates a memo. The service assembles the slot map from records only, the masking layer admits the whitelisted, non-personal fields and fails closed on anything else, the provider adapter posts to the configured endpoint — **the one hop that leaves the host** — and the shape checker validates the reply before anything is stored or shown. Pass: the memo is persisted, the model call is logged with its cost and verdict, and a stage-7 trace row is written. Fail: one retry, then refusal, with no memo record and nothing partial on screen.

**Untrusted input crosses into trusted code at exactly six doors, each validated at the door:** HTTP request bodies and parameters (Pydantic + explicit rules), uploaded documents (type, size, virus scan, parse), connector and feed payloads (schema, mapping, quarantine), configuration and threshold files (typed load with startup refusal), the model provider's replies (shape checks, always treated as untrusted), and the API's authenticated clients (scope, rate limit, validation).

### 13.4 Scaling, and what gives way first

The design target is a single host. At 1,000 borrowers the overnight batch is minutes and the database is small. At 10,000 borrowers the batch is the first thing to feel it and the answer is job-level parallelism within the host, which the job model already supports. At 100,000 borrowers the single host gives way in this order: (1) batch duration — split the pipeline across worker processes reading a shared job queue, which the database-backed job store already permits; (2) database write throughput during ingestion — partition the event and evidence tables by month; (3) document storage — move the store behind its port to network-attached storage or object storage, which is a configuration change because the store is a port. Nothing in the mechanism changes shape at any of these steps, and none of them is a rewrite, because the ports were drawn before the first line of code.

---

## §14 Interfaces and data

### 14.1 Interfaces

| Interface | In | Out | Who may call | When the caller is wrong | When it is slow or down |
|---|---|---|---|---|---|
| **Web UI** (HTTPS, host-bound or behind the customer's reverse proxy) | Authenticated sessions, form submissions, uploads | Rendered screens | Authenticated users, by role and scope | Validation failures return the field-level message; authorization failures return the documented refusal; unknown routes return the designed 404 | It is local; §25's restart procedure applies and the health endpoint reports readiness |
| **REST API** (`/api/v1`) | Scoped API keys or session auth; JSON | JSON per the OpenAPI document | Integrating systems and the UI | Documented error envelope with a code, a message and a field path; 401/403/404/409/422/429 as specified | Rate limit responses carry retry headers; long operations are asynchronous with a status resource |
| **Model provider** (outbound, optional) | Masked, whitelisted, non-personal prompt fields only | Completions | Only the two model-using stages, through one call site | Auth failure disables the stage with a clear message and never retries with altered credentials | Timeout, one retry, then degraded mode; recorded-response replay where configured; the mechanism is unaffected |
| **Core banking / LOS / treasury connectors** (inbound, read-only) | Files, REST responses or database views per the configured mapping | Ingested records with provenance | The scheduler and the administrator's manual trigger | Schema differences refuse the run and name the difference; bad rows quarantine | Connector lag is a metric with an alert; the last good data stays in force and affected forecasts carry their data age |
| **External feeds** (inbound) | Feed payloads | Evidence items after entity resolution | The scheduler | Ambiguous entity matches queue for review rather than being asserted | That evidence family degrades alone and says so on the health view |
| **SMTP / webhooks** (outbound) | Rendered digests, signed webhook payloads | Delivery status | The notification dispatcher | Invalid recipients are reported to the administrator | Retry with backoff, then dead-letter, never silent loss |
| **Document store** (local filesystem or network share) | Uploaded and generated documents | Streamed content | The application, behind its port | Path and type validation; content-type verification | Health check reports unavailability; uploads are refused rather than lost |

### 14.2 The data model

Principal entities, their meaning and their governance. Field-level definitions live in `plan.md`.

| Entity | What it holds | Retention | Sensitivity |
|---|---|---|---|
| **Organisation, Portfolio, Branch** | The customer's structure and the scoping tree | Life of the deployment | Internal |
| **User, Role, Permission, Assignment, Session** | Identity, authorization, scope, sessions | Per the retention schedule; sessions expire | Personal (staff) |
| **Borrower** | Legal name, CIN, industry, group, related parties, contacts, promoters, guarantors, directors | Contractual + regulatory period | **Personal-class** for named individuals; **official-identity** for CIN/PAN |
| **Facility** | Type, limit, outstanding, drawing power, security, pricing, effective dates | As borrower | Financial |
| **FinancialPeriod, StatementLine** | Normalised statements with provenance and restatement lineage | Regulatory period | Financial |
| **Document, DocumentPage, DocumentSpan** | Uploaded and generated documents, extracted text, page spans, OCR confidence | Per retention schedule; encrypted at rest | Mixed; treated at the strictest class present |
| **Covenant, CovenantVersion, Exception, Waiver, CurePeriod** | The contract's terms, versioned and effective-dated, with source spans | Regulatory period; versions never deleted | Free text may embed personal names |
| **CovenantTest** | Value, threshold used, headroom, verdict, exception used, inputs, computed-at, covenant version | Regulatory period | Financial |
| **SignalEvent** | Typed, dated, sourced behavioural events with a content hash | Rolling window per §21, then aggregated | Financial-behavioural |
| **EvidenceItem** | Persistence, materiality, decay, state, supersession links | Regulatory period | Financial-behavioural |
| **Forecast, ForecastDriver, DailyPath** | Probability, confidence, crossing date, formula inputs, driver shares, the full path | Regulatory period | Derived |
| **Simulation** | Intervention applied, parameters, counterfactual path, assumptions | With its memo | Derived |
| **Memo** | Slot map with record references, drafted text, actions, verdict, provider and prompt versions, exports | Regulatory period | Mixed |
| **Case, CaseEvent, Comment, Action** | Ownership, status, SLA, history | Regulatory period | Internal |
| **AuditEvent** | Append-only, hash-chained record of everything | Regulatory period, never purged before it | Payload by reference for personal-class |
| **OverrideRecord, Disposition** | The human's disagreement and what they did instead | Regulatory period; the training corpus for N-12 | Free text |
| **TraceRow** | Per stage, per subject: inputs, outputs, decider, rule or prompt version, thresholds compared with sides, confidence, sources | Regulatory period | Derived |
| **ConfigVersion, ThresholdSnapshot** | Every configuration and threshold state, with actor and approver | Regulatory period | Internal |
| **ModelCall** | Provider, model, prompt version, tokens, latency, cost, verdict, retries | 180 days minimum | Prompts stored masked only |
| **ConnectorRun, IngestionBatch, QuarantineRow** | What ran, what it read, what it rejected, reconciliation totals | 2 years | Mixed |
| **Notification, Delivery** | What was sent, to whom, through which channel, with what result | 1 year | Personal (staff) |

**Data classification** drives §16's controls: *identifying* — promoter, guarantor, director, contact and staff names and contact details; *official-identity* — CIN, PAN and equivalent; *financial* — statements, conduct, utilisation, treasury; *free text* — clause text, override reasons, comments, memo prose, treated at the strictest class present; *derived* — everything the mechanism computes. There is no health-class or biometric data in this product, and none may be added without a documented assessment.

**Every record carries:** a stable identifier, created and updated timestamps in IST with the UTC instant stored, the actor who caused it, the request id, and — where it affects a decision — the configuration version in force. Nothing in the audit, evidence or trace stores is ever updated in place.

---

## §15 Interface and experience

### 15.1 The direction

> **Every screen is a credit case file being weighed — computed numbers in ink on paper, every claim wearing its evidence, and one amber-to-red accent that only ever means breach risk.**

The audience is credit officers, risk officers and bank inspectors: people who live in documents. The product reads as a working file, not a dashboard. This direction was chosen over an ops-triage console (which flattens the evidence story into list rows and reads as generic SaaS) and a dark data terminal (which loses contrast on the projectors and low-quality monitors this will actually be shown on, and reads as a market scanner when the job is weighing one case).

### 15.2 What the direction forbids

1. **No KPI tiles or summary-card grids.** A case file has no dashboard. Numbers live in ledger tables beside their context, never as isolated big-number tiles.
2. **No colour that is not risk.** Chrome, links, buttons, icons and states stay ink and paper. The green→amber→red scale appears only where it *means* covenant headroom state. If everything is highlighted, nothing is evidence.
3. **No chart without its ledger line.** Every trajectory or sparkline sits beside the figures it plots and the evidence rows behind them, click-through. A chart that cannot show its numbers does not render.

Also refused, permanently: gradients on near-black; glass and blur; glowing borders; bento grids; stock icon sets; a component library left at its defaults; the untouched rounded-2xl shadow card and cards nested in cards; permanent dark mode with neon accents; and the cream-plus-serif-plus-sage "tasteful default" that anti-slop advice itself created. This direction dodges the last by being denser, colder and older than a lifestyle brand: examiner, not artisan café.

No 3D, maps, WebGL or heavy motion anywhere. Nothing in this product shows space, volume, a network or a route, so every figure is flat hand-drawn SVG and no visualisation library is loaded.

### 15.3 Visual identity

- **Light palette:** paper `#F7F4EE`, ink `#191C1F`, muted ink `#5A6067`, hairline `#D9D3C7`, headroom green `#1E6B45`, watch amber `#B45309`, breach red `#B42318`.
- **Dark palette (R-36), derived from the same roles, not inverted:** ground `#14161A`, ink `#E8E4DC`, muted `#9AA1A9`, hairline `#2C3138`, headroom `#4CAF7D`, watch `#E0913A`, breach `#E8695E`. Both palettes meet the contrast floors below; neither is the "real" one.
- **Type:** headings and memo prose in a workhorse newspaper serif; data and numbers in a monospaced face with tabular figures so ledger columns align and deltas scan; UI labels in the platform sans. All three are self-hosted with the licence recorded; nothing loads from a third-party origin. Devanagari faces for Hindi are self-hosted alongside them, and the ₹ glyph is verified present in every stack at every size used.
- **Scale:** 13px data / 15px body / 18px section / 22px case title / 28px screen title; weights 400, 600, 700 only.
- **Spacing:** a 4px scale (4/8/12/16/24/32/48); 32px table rows; a 12-column grid at 1200px maximum content width.
- **Shape:** 2px radius everywhere — documents have corners, not pills; 1px hairlines; no shadows, with one exception: the why-panel drawer, because it sits *above* the document.
- **Motion:** two durations — 160ms for state changes, 240ms for the panel — and one easing curve. The horizon control is direct manipulation with no tween: the display *is* the data at the selected date. Under reduced-motion, panels appear without sliding and the control updates at discrete stops.

**Every design value lives in one token file.** A colour, duration, easing curve or spacing value typed literally into a component is a defect anyone may fix without asking, and a build check fails on it.

### 15.4 Composition

Each screen is organised around the thing being done, never a grid of things to read: the queue around *choosing whom to open*; the case file around *weighing one borrower*; intake around *checking a proposal against its source text*, side by side with the document span on the left and the fields on the right; the simulator around *comparing options against doing nothing*. Density is high in ledger tables and deliberately low above the fold — the case-file header holds exactly four facts and 40% whitespace, so the eye lands somewhere. Every number is right-aligned on the decimal in the data face. The accent may appear only in the headroom column, the forecast band, the crossing tick and the band chips — never in navigation, buttons or headings.

Every mockup, screenshot and test fixture uses realistic Indian content lengths — "Meridian Auto Components Private Limited", "₹62.4 crore", "TOL/TNW ≤ 3.25" — never lorem or short placeholders, because a layout that only survives short names is a layout that has not been designed.

### 15.5 The signature interaction

**The horizon control.** Moving from today toward the maximum horizon, the covenant trajectories deplete, the crossing tick appears the moment a line meets its threshold with the date attached, the drivers ink themselves into the margin as they begin to bite, and the header's dated risk rewrites live. It reads stored daily paths and never re-models, which is what makes it fast enough to feel like direct manipulation. A tab strip in its place would show three static snapshots; this shows trajectory, when, and what is driving it, in one gesture. It is fully keyboard-operable — arrows step days, Home and End jump to the ends, the named horizons are tab stops — and it degrades to those fixed stops with JavaScript disabled or reduced motion set.

### 15.6 Accessibility, locale and states

WCAG 2.2 Level AA is a release gate (N-07), not an aspiration: contrast at least 7:1 for text and 4.5:1 for accent chips in **both themes**, full keyboard operability, visible focus, semantic headings and landmarks, real table semantics, labelled controls, a text equivalent for every figure (the ledger line under forbid 3 doubles as the accessible rendering), reduced-motion support, and usable layout at 200% zoom and 320px width. Interactive targets are at least 32×32px, and the horizon handle is 44px at narrow widths.

English and Hindi ship (R-35), with Indian number formatting, IST dates and Indian FY quarters in every locale. Every screen has designed empty, loading, error and degraded states: empty states name the next action; loading uses skeleton ledger rows rather than a spinner over blank; error states name what to do next; degraded states say precisely which capability is unavailable and confirm that everything computed locally still works.

---

## §16 Security, privacy and compliance

### 16.1 Identity and access

**How the product knows who is calling.** Every request carries a session established by one of two authentication paths: the customer's identity provider over OIDC or SAML, or a local user store with Argon2id password hashing, a configurable policy, optional TOTP second factor, lockout after repeated failure and forced rotation on first login. Sessions are signed, HttpOnly, SameSite cookies with idle and absolute timeouts, invalidated on password change, role change and logout, and enumerable and revocable by an administrator. Service-to-service callers use scoped API keys with per-key rate limits, rotation and revocation.

**What each role may do** — enforced in the application's authorization layer, verified by a test per role per endpoint (N-04.b), and additionally defended by row-level scoping in the query layer. `✗` means the permission is absent; `✗✗` means no role holds it in any configuration.

| Action | RM | Credit | Cr. Approver | Risk | Risk Head | Auditor | Admin | Steward |
|---|---|---|---|---|---|---|---|---|
| View queue, case files, memos (within scope) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Upload documents, run intake | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ |
| Register / amend a covenant | ✗ | ✓ | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Approve a covenant registration | ✗ | ✗ | ✓ | ✗ | ✓ | ✗ | ✗ | ✗ |
| Confirm a covenant that failed verification | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ |
| Record a waiver | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Generate memo, run simulation | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Log an action, update a case | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Override a risk view (reason mandatory) | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ | ✗ | ✗ |
| Propose a threshold change | ✗ | ✗ | ✗ | ✓ | ✓ | ✗ | ✓ | ✗ |
| Approve a threshold change | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| Manage users, roles, connectors, jobs | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ |
| Approve a model promotion | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| Resolve quarantine, correct source data | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| Export an evidence bundle | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| Read personal-class fields in the clear | scoped | scoped | scoped | scoped | scoped | ✓ logged | break-glass, logged | scoped |
| Edit or delete an audit event | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ |
| Approve a credit action, waiver or escalation *in the tool* | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ | ✗✗ (P-01) |

Two rows carry `✗✗` for every role in every configuration and have no flag: confirming a covenant that code could not verify, and taking a credit decision inside the tool. Both are structural — the first has no code path, the second has no endpoint.

### 16.2 Data protection

| Class | In logs | On the wire | At rest | On deletion | Leaving the host |
|---|---|---|---|---|---|
| Identifying (promoter, guarantor, director, contact, staff) | Never in the clear; masked to role tokens | TLS only; loopback or the customer's network | Encrypted at column level with a key outside the database | Purged by the retention job or an erasure request; audit events redacted with the chain preserved | **Never.** The outbound whitelist has no key that carries them and fails closed |
| Official-identity (CIN, PAN and equivalents) | Never; masked to opaque tokens | TLS only | Encrypted at column level | As above | Never — pattern-stripped before any outbound call |
| Financial (statements, conduct, utilisation, treasury) | Aggregates only | TLS only | Database-level encryption; column encryption where the customer requires it | Per the retention schedule | Only as masked derived values (ratios, headroom, scores) when a model stage is enabled |
| Free text (clause text, override reasons, comments) | By reference id, never the body | TLS only | Encrypted at column level | As above | Clause text only, after masking, and only for stage 1 |
| Derived (tests, evidence, forecasts, drivers) | Values may appear | TLS only | Standard | Per the retention schedule | As masked values for stage 7 |
| Secrets | Never, and a scanner enforces it | Never transmitted | Environment or OS keyring; never a file in the tree | Rotated per the procedure | Never |

**Keys and secrets.** Every credential — database, model provider, SMTP, connectors, webhook signing, session signing, field-encryption key — is read from the environment or the operating system's secret store. None is ever committed, logged, printed, screenshotted or included in an export, and pre-commit and CI scanners verify it across the whole history. Rotation is documented for each and requires no code change.

**TLS.** Verification is on, always. A released build has no configuration that disables certificate verification, and a test asserts it. Deployments behind a corporate proxy install the corporate CA bundle; that is the supported path and the only one.

### 16.3 Application hardening

Content Security Policy with no external origin permitted; HSTS, X-Content-Type-Options, Referrer-Policy and frame-ancestors set; CSRF tokens on every state-changing form; output escaping by default with any raw rendering explicitly justified and reviewed; parameterised queries only, verified by a static check; upload validation of type, size, extension and magic bytes plus a virus scan before the file reaches the store; response size and pagination limits; rate limiting on authentication, on password reset and on the API; timing-safe comparison in every authentication path; and generic authentication error messages that do not reveal whether an account exists.

### 16.4 What an auditor is shown, and where it lives

The warning reconstruction (R-20.a) · the evidence bundle export with its verified manifest (R-20.c) · the audit event search with the hash-chain integrity report (R-20.b) · the model-call log with verdicts and cost (N-02.b) · the threshold and configuration history with actor and approver (N-03.a) · the outbound-capture scan proving no personal data left the host (N-04.a) · the role matrix above with its per-endpoint tests (N-04.b) · the data inventory and retention schedule with the purge log (N-11) · the model registry with approvals and drift reports (N-12) · and the compliance evidence pack mapping every obligation in §2.1 to the control and the passing test that proves it (N-11.d).

---

## §17 The AI layer: guardrails, grounding, explainability and evaluation

### 17.1 What the model may decide, and what it may never decide

**The model may:** propose covenant fields for stage 1, where code and a human stand between the proposal and the record; and write the connecting prose of stage 7's memo over facts it is given and cannot change.

**The model may never:** compute a ratio; decide a pass or fail; set or alter a probability, confidence, date, threshold, band or ranking; invent a figure, name, date or citation; decide an escalation; recommend an action outside the catalogue; or take any action at all. Any figure, name, date or citation the product presents as true comes from a record or a calculation. Text the model genuinely wrote is displayed labelled **Drafted by model**, so a user always knows which of the two they are reading.

This is enforced by construction, not by instruction: the domain layer cannot import the provider (an import-linter contract fails the build), the memo's figures are injected into named slots from records, and any numeric token in the prose that does not appear in a slot fails the shape check and refuses the memo.

### 17.2 Grounding and the shape contract

Everything shown traces to something the product can point at: the statement line it read, the record it loaded, the sum it computed. Where there is nothing to point at, the product says so — "insufficient evidence — watching", "not computable — flagged for review", "data to Q2 FY26 — covenant marked stale" — instead of filling the gap.

**Stage 1** must return an object with exactly the specified fields; the checker verifies schema validity, that the definition is in the ratio library or is a valid custom formula, that the definition is independently recomputable against this borrower's stored statements, that the threshold is inside the plausible band for that ratio type, that units and currency are consistent, and that the frequency and effective dates are allowed and consistent with the facility.

**Stage 7** must fill the fixed template; the checker verifies that every figure slot resolves to a record reference, that every recommended action exists in the catalogue with the right role tag, that length is within the ceiling, that no numeric token appears outside a slot, and that no forbidden directive language appears.

**An answer that fails is retried once and then refused — never displayed, never partially displayed.** A refused stage-7 answer writes no memo record, so a half-finished memo does not exist. This shape check is the product's own code, and it remains the product's own code even where a provider offers structured output, because a guardrail in code is not a request in a prompt.

### 17.3 Prompt and provider governance

Prompts are versioned files whose version string is embedded in the file and checked against the filename before any call; a prompt edit without a version bump fails the build (N-12.b). Every call records the provider, the model identifier the provider returned, the prompt version, tokens in and out, latency, computed cost, the check verdict, the retry count and any refusal reason. A per-hour and per-day call ceiling and a monetary budget ceiling are configuration values; reaching one queues rather than drops, and says so on screen. All provider interaction goes through one call site; nothing else in the codebase may perform an outbound model call, and a test asserts it.

### 17.4 Guardrails in both directions

**Outbound:** the masking layer admits only whitelisted, non-personal keys and raises on anything else — it fails closed, so a new field cannot leak by being forgotten. Names become role tokens; identifier patterns become opaque tokens; the token map stays on the host so the stored record keeps the original. The configured secret value is scanned for and redacted from any assembled prompt, so a pasted credential cannot travel.

**Inbound:** input that attempts to redirect the model receives the fixed refusal and a security audit event, enforced by the template structure and the code shape check rather than by asking the model politely. Refused outright regardless of phrasing: credit decisions, waiver approvals, fraud classification, and any output about an entity not in the monitored book.

### 17.5 Decision thresholds

Every number the product compares against to decide lives in versioned configuration (N-03), is changeable without a code change, requires approval, is snapshotted on change, and is referenced by every record that used it. A threshold with no stated boundary behaviour is a coin toss written as a number, so every one below names its boundary.

| # | Threshold | Default | Above | Below | Exactly at |
|---|---|---|---|---|---|
| T1 | Escalation probability at the worst covenant-horizon | act ≥ 0.70; amber ≥ 0.40 | Act: queue top, case raised, memo offered | Watch: listed, no escalation | The boundary belongs to the higher band |
| T2 | Confidence floor for showing a probability | 0.50 | Probability shown with its confidence | "Insufficient evidence — watching" with the reason; no bare probability renders anywhere, including the API | Shown — the floor is inclusive |
| T3 | Persistence: sustained if | ≥14 consecutive days **or** ≥3 events in 30 days | Sustained: feeds forecast pressure | Transient: visible, decaying, no forecast effect | Sustained — inclusive |
| T4 | Materiality: evidence counts if projected 90-day headroom erosion is | ≥5% of the threshold value | Counts toward pressure | Noted on the ledger, excluded from pressure | Counts — inclusive |
| T5 | Driver listed if contribution is | ≥10% of the risk delta | Listed with its share | Folded into `other` | Listed |
| T6 | Memo length ceiling | 1,200 output tokens | Regenerate once shorter, else refuse | Fine | Fine |
| T7 | Model calls per hour, per day, and monetary budget per month | 200 / 2,000 / configured | Queued, with a banner and an alert | Fine | The ceiling engages at the value |
| T8 | Bad-shape retries | 1, then refuse | — | — | — |
| T9 | OCR page confidence floor | 0.80 | Page text used | Page routed to human review | Used — inclusive |
| T10 | Entity-match confidence floor | 0.90 auto-accept; 0.60 review floor | Auto-accepted | Below 0.60 discarded; between, queued for review | Auto-accepted at 0.90 |
| T11 | SLA hours by band | act 24h, amber 72h, watch 168h | Overdue: escalates and appears in the digest | Within SLA | Overdue at the value |
| T12 | Batch completion deadline | 07:00 IST | Alert raised, run continues | Fine | Alert at the deadline |

Defaults are engineering starting values calibrated on the reference portfolio; the calibration procedure for a customer's own book is documented and is a pilot activity (**[OPEN-06]**).

### 17.6 Explainability

For every decision a user sees, they can open **why**: which inputs were used, which sources were read, which stage made the call, how sure it is and what that number means, and what would have changed the answer. Every stage of §3's mechanism opens — not only the model call — and each kind of stage answers the same questions in its own terms. A **code stage** shows the rule identifier, its version, the numbers it compared, the thresholds it compared them against and which side each value fell. A **model stage** shows the masked, versioned prompt, the text returned, and the code verdict on it. A **statistical stage**, if one is ever promoted under N-12, shows which inputs moved the decision, by how much, and which model version decided — the same panel, the same contract, no second surface. A stage that did not run says so.

### 17.7 Evaluation, and the loop back

The evaluation set (N-01) is versioned in the repository, grows by adding a file rather than changing code, and covers every stage. Every example carries a hand-labelled expectation and a pass mark. The harness runs the product arm and the baseline arm on every commit against recorded model responses, prints both scores against the marks, and fails the build when a score falls below its recorded floor. The pass marks are: engine and boundary cases exact; extraction field precision and recall ≥0.95 after verification with 100% fail-closed on the adversarial cases; forecast dating within ±10 days on the labelled cohort with zero false escalations on the stable and noisy cohorts; grounding with zero ungrounded figures; refusal correct on every malformed reply; and memo usefulness at or above the rubric floor scored by three independent reviewers per release.

**The loop back.** Every correction, override, rejection, disposition and acceptance is recorded with what was shown, what the user did instead, which stage produced it, the versions in force and the thresholds in force (R-19). That record does three things in production: a disputed forecast becomes a candidate evaluation example; a repeated extraction correction becomes a prompt revision with a version bump and a harness run; and a threshold complaint becomes a calibration proposal that goes through approval. Over a quarter it accumulates into the labelled dataset that N-12's challenger is trained and judged on — with a named risk owner approving any promotion, the harness gating it, drift monitored afterwards, and rollback available by pinning the prior version. A product that collects nothing has no second version.

---

## §18 Performance and capacity

Targets are measured on the reference hardware — 8 vCPU, 32 GB RAM, NVMe storage — against the reference portfolio of 5,000 borrowers, 12,000 facilities, 30,000 covenants, 8 quarters of financials and 365 days of daily signals. Every row is measured per release and published (N-05).

| Target | Number | Measured how |
|---|---|---|
| Any screen render, warm cache | p95 ≤ 800 ms, p99 ≤ 1.5 s | Scripted load over the reference portfolio |
| Queue with filters over 10,000 rows | p95 ≤ 1.2 s first page | Same |
| Horizon control step | ≤ 100 ms — it reads stored daily paths | Browser performance trace |
| Intervention simulation | ≤ 1.5 s per intervention | Scripted |
| API read endpoint | p95 ≤ 300 ms | Load test |
| Document ingestion, native-text 40-page PDF | ≤ 20 s to indexed spans | Timed |
| Document ingestion, scanned 40-page PDF with OCR | ≤ 3 min | Timed |
| Covenant intake proposal (provider round trip excluded) | ≤ 2 s of local work | Timed |
| Memo generation end to end | p95 ≤ 25 s including provider latency | Timed over repeated runs |
| Overnight batch, reference portfolio | ≤ 45 min, completing before 07:00 IST | Job ledger |
| Overnight batch, 10× reference (50,000 borrowers) | ≤ 4 h with job parallelism | Load test; published even where it misses |
| Statement import, 100,000 rows | ≤ 10 min with the reconciliation report | Timed |
| Concurrent interactive users | 50 sustained, 100 peak, within the latency targets | Load test |
| Database size growth | ≤ 6 GB per 1,000 borrowers per year including evidence and traces, excluding documents | Measured and extrapolated |
| Availability | 99.5% monthly during business hours | Production telemetry |
| Recovery point objective | ≤ 15 minutes | Rehearsed restore |
| Recovery time objective | ≤ 2 hours | Rehearsed restore |

**The scaling story in one sentence:** the mechanism costs zero model calls per borrower per day by design — the model is used only at covenant intake, which is human-paced, and at memo generation, which is on demand and cached per borrower-day — so the cost of monitoring scales with CPU, not with tokens.

---

## §19 Edge cases and failure behaviour

The principle: **fail closed, say what happened, name the next step, lose nothing, and never invent a value to fill a gap.** Nothing is retried silently except where a count is stated.

| Case | What the user is told | Retried | Must never be lost |
|---|---|---|---|
| Empty or whitespace clause input | "Paste or select the covenant text first." | No | — |
| Oversized clause input | "Too long for one covenant — submit one clause at a time." Text is not stored | No | — |
| Instruction-injection in any text sent to a model | Fixed refusal; a security audit event is written | No | The audit event |
| Hostile input in any field | Escaped and validated; "That value isn't valid here" with the field named | No | — |
| Malformed or truncated import file | The failing row and rule are named; valid rows load; bad rows quarantine | No | The valid rows and the quarantine |
| Duplicate delivery of any file or batch | "Already ingested" with the prior run named; nothing duplicates | — | Idempotence |
| Source schema changed | The run refuses, naming the difference | No | The last good data |
| Missing quarter for a borrower | "Data to Q2 FY26 — covenant marked stale"; confidence reduced | No | The staleness marker in the trail |
| Ratio undefined | The defined not-computable message with its reason, flagged for review | No | The flag |
| Password-protected or corrupt document | The failing page is named; the upload is rejected | No | — |
| OCR below the confidence floor | The page is flagged for human review rather than used | No | The flag |
| Ambiguous external entity match | Queued for review, never asserted | No | The queue item |
| Model provider slow | Timeout at the configured limit, then the down path | 1 | The call log entry |
| Model provider down or auth failed | "Model-assisted features are unavailable. Everything computed locally still works." Hand entry and template memo remain | No further | Everything computed locally |
| Model returns 200 with junk | Treated exactly as a failed shape check | Per T8 | The verdict in the call log |
| Model budget or rate ceiling reached | "Model-call ceiling reached — queued"; an alert fires | Queued | The queue |
| SMTP or webhook delivery failure | Retry with backoff, then dead-letter with an administrator alert | 3 | The message |
| Batch job failure | The job's error is visible, the run alerts, the portfolio is never left half-scored | Configured | Prior results |
| Batch misses its deadline | An alert fires; the run continues; affected data ages are shown | — | — |
| Database unavailable | A maintenance page with the incident reference; the health endpoint reports it | Connection retry with backoff | Committed data |
| Disk full | Writes refuse cleanly with an alert; nothing is half-written | No | Committed data |
| Concurrent edit of the same record | Optimistic concurrency; the second writer is told what changed and by whom | No | Both intentions |
| Session expiry mid-form | The user is returned to their input after re-authentication | — | The unsaved input |
| Crash or restart mid-batch | Jobs resume from the ledger; no partial state survives a transaction boundary | — | The audit trail |
| Retention purge encountering referenced data | The purge respects references, records what it kept and why | — | Referential integrity |
| Clock skew detected | An alert fires; SLA computation is suspended rather than computed wrongly | — | — |

---

## §20 Observability and operations

**Application log** (structured JSON, rotated, 180-day in-country retention): timestamp, request id, route, method, status, duration, principal, outcome, error class, and business context by reference. **Never logged:** secrets, personal-class values in the clear, full document or clause bodies, model prompt bodies.

**Model-call log** (separate stream): call id, request id, stage, provider, model identifier as returned, prompt version, tokens in and out, latency, computed cost, check verdict, retry count, refusal reason. This exists because the application log answers nothing about "why did it say that" — the prompt version pins what was asked, the verdict pins what happened to the answer, and the cost field turns a month of usage into a number the customer can see.

**Audit stream** is separate again and is a business record, not a log (R-20): it is never rotated on time alone, never sampled, and never written to the same sink.

**Metrics:** request latency and error rate by route; authentication successes and failures; job duration, outcome and lag by job; queue depth and band distribution; evidence volume by family and state; forecast count and confidence distribution; model latency, token, cost and refusal rate; connector lag and quarantine depth; notification delivery success; database connection pool saturation; document store usage.

**Traces:** one span per request, propagated through service, repository, provider and job boundaries, sampled at a configurable rate with errors always sampled.

**SLOs and the numbers that wake someone:**

| Signal | Objective / alert threshold | Who is woken | Runbook |
|---|---|---|---|
| Availability during business hours | 99.5% monthly; page below 99% | On-call | RB-01 |
| Screen p95 latency | ≤ 800 ms; alert at 1.5 s for 10 minutes | On-call | RB-02 |
| Overnight batch completion | Before 07:00 IST; page on miss | On-call | RB-03 |
| Job failure rate | 0 unretried failures; page on any | On-call | RB-04 |
| Connector lag | ≤ 1 cycle; alert at 2 | Data steward | RB-05 |
| Quarantine depth | Alert above the configured count | Data steward | RB-06 |
| Model shape-check failure rate | Alert above 5% in an hour | AI owner | RB-07 |
| Model provider p95 latency | Alert above 20 s | AI owner | RB-08 |
| Model spend against budget | Alert at 80% of the monthly ceiling | AI owner | RB-09 |
| Override rate | Alert above 30% of warnings shown in a week — the idea-risk alarm | Risk head | RB-10 |
| Amber-or-worse share of portfolio | Alert above 10% on any scoring day — the alert-fatigue guard | Risk head | RB-11 |
| Authentication failure spike | Alert on the configured rate | Security | RB-12 |
| Audit chain integrity check | Any failure pages immediately | Security | RB-13 |
| Backup success and restore rehearsal | Daily success; quarterly rehearsal | Administrator | RB-14 |
| Log write failure | Any occurrence alerts | Administrator | RB-15 |
| Certificate expiry (TLS, SAML, API keys) | Alert 30 days ahead | Administrator | RB-16 |

Every alert names a runbook section that exists (N-10.b). A cyber incident starts the CERT-In six-hour reporting clock, and RB-12 and RB-13 carry that clock in their first line.

---

## §21 Data lifecycle, retention and compatibility

**Retention schedule**, enforced by a scheduled job with a purge log (N-11.b):

| Data | Retention | Then |
|---|---|---|
| Audit events, trace rows, covenant versions, tests, memos, overrides, dispositions | The regulatory period the customer configures, minimum 8 years | Archived to cold storage with the chain intact; never deleted before the period |
| Documents (source and generated) | As the records that reference them | Archived; the reference resolves to the archive location |
| Financial periods and statement lines | 8 years | Archived |
| Raw signal events | 24 months | Aggregated into monthly summaries; evidence items derived from them survive |
| Forecasts and daily paths | 24 months at full daily granularity | Downsampled to the horizon summary; the trace row survives |
| Application and model-call logs | 180 days minimum (CERT-In), configurable upward | Rotated and archived in-country |
| Notification delivery records | 12 months | Purged |
| Sessions | Expiry plus 30 days | Purged |
| Quarantine rows | 90 days after resolution | Purged |

**Erasure.** A data-principal erasure request removes personal content from business records and replaces it with a redaction marker, leaves every audit event chain-valid with its personal payload redacted rather than removed, records the request and its execution as audit events of their own, and produces a completion certificate. The procedure is documented, tested and part of the compliance pack (N-11.c).

**Schema evolution.** Migrations are versioned, forward-only, and tested for upgrade from the prior release with representative data (N-09). Breaking changes to the API follow the versioning and deprecation policy in R-32, with a minimum of one release of overlap. Changes to a covenant's stored representation never rewrite history: a new version is created and old records continue to resolve against the version they used. Import formats and connector mappings are versioned, and a mapping change is a new mapping version rather than an edit.

**Backup and restore.** Full nightly and incremental hourly backups of the database, the document store and the configuration, retained per the customer's policy, encrypted, with a restore rehearsed quarterly and timed against §18's objectives (N-06.a). A restore that has never been rehearsed is a hope, not a control.

---

## §22 Customer evaluation, pilot and acceptance

The 0.1 draft had a five-minute judged demo. This release has a customer who will run the software against their own book before they pay for it. The sequence below replaces the demo entirely.

### 22.1 Evaluation build

A self-contained evaluation installation loaded with the reference portfolio — a deterministically generated synthetic Indian commercial-lending book of 5,000 borrowers with authored deterioration, noisy-transient and stable cohorts and labelled outcomes. It installs from the standard installer in under an hour, requires no customer data and no outbound network, and demonstrates every capability in §8.1 with the model-using stages running against recorded responses where no provider is configured. It exists so an evaluator can form a judgement in a day without a project.

The evaluation walkthrough, in the order a credit officer would actually work: the morning queue already scored overnight → a case file with the horizon control and a dated crossing → the why-panel opened on a stage that no model touched, showing the rule, the numbers, the thresholds and which side each value fell → a signal day advanced so a transient blip visibly decays while a sustained pattern escalates → a sanction letter uploaded, clauses proposed, one struck by the verification with the failing check named, the rest confirmed and tested on the spot → three interventions simulated side by side against doing nothing → a memo generated, every figure clicked through to its record, exported to PDF → an override logged with a reason and both states retained → the reconstruction of that warning exported as an evidence bundle → the evaluation scoreboard showing the product arm and the baseline arm on the same examples.

### 22.2 The questions a serious evaluator asks, and where the answer is

1. *"Where does that probability come from?"* — the inspectable formula, opened live in the why-panel with the thresholds named and the sides shown; calibrated on the reference portfolio, recalibrated on yours in pilot (§17.5, §17.6).
2. *"What happens on the input you didn't show me?"* — it fails closed: proposals are struck with the failing check named, stale data is marked and reduces confidence, OCR below the floor routes to a human, ambiguous matches queue. Every case is in §19 with a test behind it.
3. *"Why a model at all — rules could do this?"* — rules *do* stages 2 through 6. The model exists only where language is unbounded, and the scoreboard shows both arms on the same examples: the regex parser's misses are the answer (§17.7).
4. *"What did you make up for the demo?"* — nothing is stubbed. The reference portfolio is synthetic and labelled as such; every computation is the production code path; model stages run live or against recorded responses, labelled either way (§22.1).
5. *"What does this cost us to run at our size?"* — monitoring costs zero model calls per borrower per day by design; the bill is intake and memos on demand, with tokens and cost logged per call and a budget ceiling in configuration (§17.3, §18).
6. *"Why you and not the incumbent we already have?"* — they alert on entity risk or track compliance dates. Radar recomputes the signed contract, dates the breach, simulates the intervention and opens every stage to audit. And Radar consumes their signals as one more evidence family rather than replacing them (§4, R-30).
7. *"Can our inspector reconstruct a warning from eighteen months ago?"* — yes, including the covenant version, the thresholds in force, the evidence that existed then and the model call that drafted the memo, exported as a verifiable bundle (R-20).
8. *"What is the weakest claim in this document?"* — §22.4. It is named there rather than found here.

### 22.3 Pilot

A pilot runs for two quarters on a defined cohort of the customer's own accounts, in shadow mode beside the existing QIS process, binding nothing:

- **Weeks 1–2 — installation and onboarding.** Install, connect one read-only extract, import the cohort's history, intake or import the covenant register, run the historical backfill, reconcile.
- **Weeks 3–4 — calibration.** Backtest against the cohort's known outcomes; tune thresholds and forecast weights against G1 and G3 together, never separately; record every change with its justification (**[OPEN-06]**).
- **Weeks 5–20 — shadow running.** Daily batch, alerts to the credit team, dispositions recorded, no binding decisions. Weekly review of escalations and dismissals with the risk desk.
- **Exit measurement:** G1 lead time achieved on real accounts; G3 escalation volume and false-escalation rate against the desk's own judgement; G6 time-and-motion against the manual baseline on matched accounts; G4 usability with the customer's own staff; G2's recovery arithmetic on any account where an intervention was taken.
- **Exit decision:** production rollout to one desk with tuned thresholds, then portfolio-wide.

### 22.4 The single weakest claim, named first

The mapping from headroom, velocity and evidence pressure to a probability is calibrated on synthetic histories and, until a pilot completes, has never met a real Indian portfolio. We make the claim anyway because the direction is evidenced (SMA-2 stress leads slippage, §2 row 9), because the mechanism is transparent enough to recalibrate on real data without redesign, and because the honest alternative — showing headroom and velocity with no probability — is the baseline arm we print beside it. In pilot, until calibration completes, the product shows its confidence and its lead time and lets the desk judge; it does not present an uncalibrated probability as a measured one. If a customer finds this before we name it, it costs the deal; named, it is a design decision with a plan attached.

---

## §23 Acceptance criteria for the 1.0 release

The release is done when every line below is true on the release candidate, verified on a clean installation of the target platform, and recorded in the release evidence pack.

1. **Every requirement's checks pass.** R-01 through R-36 and N-01 through N-12, each check executed by its named method, results recorded, zero unaddressed failures. Any check waived requires a named owner's written acceptance in the pack.
2. **Quality gates green.** Lint, format, type-check (strict on the domain), import-linter contracts, unit, property, integration, contract, migration, end-to-end, authorization, accessibility and performance suites all pass in CI on the release commit. Domain coverage ≥90% line and ≥85% branch. Mutation score at or above its floor.
3. **Evaluation scored and published.** The harness runs both arms on the full example set, every pass mark is met or the miss is documented with an owner, and the scoreboard ships in the release notes.
4. **Security signed off.** Penetration test complete with all high and critical findings closed or accepted in writing; dependency and secret scans clean; the authorization matrix verified per role per endpoint; the outbound capture shows zero personal-class fields and zero secret material; TLS verification cannot be disabled.
5. **Compliance pack complete.** Every obligation in §2.1 maps to a control, a requirement and a passing test. Data inventory, retention schedule, purge log, erasure procedure and model registry all present and current.
6. **Operations proven.** Clean install on a fresh host inside the documented time, performed by someone who did not write the installer. Upgrade from the prior release preserves data and passes verification. A deliberately failed upgrade rolls back automatically. Backup and restore rehearsed and timed within §18's objectives. Every alert rule has a runbook section.
7. **Performance measured.** Every §18 row measured on the reference hardware and published, including any miss.
8. **Interface proven.** WCAG 2.2 AA audit clean on every screen in both themes and both languages; screen-reader walkthrough of the primary flows recorded and its findings closed; usability sessions run with ≥5 participants from the customer's desk and G4's marks met or the gap documented.
9. **Documentation complete.** Administrator guide, user guides, API reference, runbook, architecture decision records, model cards and release notes, each reviewed by someone who did not write it.
10. **Evaluation build reproducible.** The reference portfolio regenerates deterministically, the evaluation installation builds from the tag, and §22.1's walkthrough completes end to end.

If someone can argue about whether an item passed, the item is written wrongly. Each points at a check, a measured number or an artefact that exists or does not.

---

## §24 Testing and rollout

### 24.1 How each requirement is proved

| Requirement group | Method | Where the checks live |
|---|---|---|
| R-01, R-02, R-03, R-05, R-07, R-08, R-09, R-10, R-11, R-12, R-13, R-14, R-15, R-16, R-18, R-19, R-20, R-28 | Automated unit and integration tests encoding every check, plus property-based tests for the ratio library, ledger scoring and forecast invariants; run on every commit | The domain and integration suites |
| R-04, R-06, R-17 (model and document stages) | Automated against recorded responses and fixture documents through the evaluation harness; adversarial and failure paths tested explicitly; a scheduled live-provider suite runs against the real endpoint outside the build gate | Harness plus the AI suites |
| R-21, R-22, R-23, R-24, R-25, R-26, R-36 | End-to-end browser tests for the primary flows in both themes and languages, plus automated accessibility audits and manual screen-reader review | The e2e suite |
| R-27, R-29, R-30, R-31, R-32 | Integration tests against local doubles — an SMTP sink, a webhook receiver, fixture files, a recorded REST source — plus contract tests against the OpenAPI document | The integration and contract suites |
| R-33, R-34, R-35 | Automated tests including a scope-leak test per surface and a missing-translation build check | The integration suite |
| N-01 … N-12 | Each carries its own automated gate; the ones that cannot be automated — the restore rehearsal, the install by a fresh operator, the screen-reader walkthrough, the penetration test — are scheduled activities with named owners and dated evidence | CI plus the release checklist |

**Tested with real Indian shapes.** Even where data is synthetic, its shapes are real: clause texts in Indian sanction-letter idiom with ₹ crore thresholds, TOL/TNW, FY quarters and exception windows; ratio ranges calibrated to RBI DBIE aggregates; ₹ lakh/crore formatting; IST dates; CIN-format identifiers; realistic entity name lengths. Extraction, display and layout meet the text they will meet in production.

### 24.2 Rollout

1. **Evaluation** — the self-contained build (§22.1) at the customer's site, no customer data.
2. **Pilot, shadow mode** — one cohort, two quarters, binding nothing (§22.3).
3. **Limited production** — one desk, tuned thresholds, alerts binding on that desk's process, with a weekly review and a named rollback decision-maker.
4. **Portfolio-wide** — after a limited-production quarter with the SLOs met and the escalation volume accepted by the risk head.
5. **Steady state** — quarterly recalibration, quarterly restore rehearsal, monthly SLO and drift review, and a release train with a documented upgrade window.

Each transition has an entry gate with a named approver and a documented rollback. No transition is automatic on a date.

---

## §25 Installing, operating and recovering

### 25.1 Installation

The installer provisions the runtime and the application virtual environment, creates or connects the PostgreSQL database, runs migrations, registers the service (Windows Service or systemd unit), creates the log and document directories with correct permissions, configures TLS, seeds reference data, creates the first administrator with a forced password change, and runs a post-install verification that exercises database connectivity, migrations, the health endpoint, the scheduler, the document store, the mail relay if configured and the model provider if configured — reporting each as pass, fail or not-configured.

Configuration is a single documented file plus environment variables for every secret. The file is validated at startup, and an invalid value refuses the start with the key and line named rather than starting in a wrong state.

A container image and compose file ship as an alternative for customers who prefer them. They are supported, never required, and produce an identical application.

### 25.2 Operating

Day-to-day operation is: watch the job history and the SLO dashboard, review the quarantine and the entity-match queue, act on alerts through the runbook, and rotate credentials on the documented schedule. Everything an administrator needs is in the console (R-26); nothing requires editing the database by hand, and doing so voids the audit chain's integrity guarantee.

### 25.3 Upgrade and rollback

Upgrade is one command: it backs up, stops the service, migrates, starts, verifies, and on any failure restores the backup and restarts the prior version automatically, leaving a working system and a report. A manual rollback procedure exists for the case where the automatic one cannot run, and both are rehearsed before each release (N-08.c).

### 25.4 Support and incident response

A documented severity taxonomy with response commitments; a diagnostic bundle command that collects logs, configuration with secrets redacted, version metadata and health output for support; the CERT-In six-hour clock embedded in the security runbook sections; and a customer-facing incident communication template.

---

## §26 Risks and mitigations

| ID | Risk | Likelihood | Impact | Early warning | Mitigation | If it happens |
|---|---|---|---|---|---|---|
| RISK-01 | Forecast probabilities are not believed by domain experts because calibration is synthetic until pilot | Medium | High — it is the headline claim | Override rate above 30% (RB-10); disbelief in pilot review | Transparent formula, confidence always shown, baseline arm printed beside it, §22.4 names the weakness first, calibration is a scheduled pilot activity | Lead with the deterministic engine and the dated headroom, which are arithmetic; present probability as a ranking aid until calibrated; the why-panel is the rebuttal |
| RISK-02 | Materiality and persistence control fails and the product escalates noise | Medium | High — G3 is a named goal and alert fatigue kills adoption | Amber-or-worse share above 10% on any scoring day (RB-11); dismissal rate rising | T3/T4 tuned against the paired goals on every harness run; the evaluation set carries a noisy cohort; dismissal telemetry is a first-class metric | Recalibrate through the approved threshold path; the revision flow is itself a feature; add the misclassified cases to the evaluation set |
| RISK-03 | Extraction accuracy on real sanction letters is below the mark, especially on scans | Medium | Medium | Field precision on the customer's own documents during onboarding | Code re-verification means a wrong extraction is refused rather than registered; OCR confidence floor routes bad pages to humans; hand entry is always available on the same screen | Onboarding shifts weight to hand entry with assisted pre-fill; the failure is visible, not silent; document-type-specific prompts are added and version-bumped |
| RISK-04 | Customer data quality is worse than assumed — missing quarters, inconsistent units, no daily conduct feed | High | High — it degrades every downstream stage | The onboarding reconciliation report and the quarantine depth | Provenance, quarantine, restatement support and staleness marking are built in; confidence falls visibly rather than silently; the reconciliation report is an onboarding deliverable, not a surprise | Scope the pilot cohort to accounts with usable data, name the gap to the customer with its effect on confidence, and treat data remediation as a joint workstream |
| RISK-05 | Model provider availability, cost or terms change | Medium | Medium — stages 1 and 7 only | Provider latency and error alerts (RB-08); budget alert (RB-09) | The provider layer is the exit strategy; adapters exist for alternatives; the mechanism costs zero model calls per day; degraded mode is designed and tested | Switch adapter by configuration; run degraded with hand entry and the template memo; the monitoring capability is unaffected |
| RISK-06 | The single-host architecture is outgrown by a large customer | Medium at 100k facilities | Medium | Batch duration trending toward its deadline (RB-03); database growth | §13.4's ordered give-way list; job parallelism already supported; document store already behind a port; table partitioning planned | Execute the documented steps in order; none is a rewrite |
| RISK-07 | Regulatory expectations change — new EWS, AI or data-protection rules | Medium | Medium | Regulatory monitoring; the annual compliance review | Thresholds, catalogues, reports and retention are configuration; explainability and audit exceed today's requirements deliberately | Configuration change plus a report template; the compliance pack maps the new obligation to an existing control |
| RISK-08 | Scope grows during pilot as the customer asks for their own report, field or connector | High | Medium | Change requests during pilot | Connector, feed, catalogue, report and threshold extensibility are designed in so most requests are configuration; §29 governs specification changes | Configuration where possible; a change to this document with an effort estimate where not; never a silent scope absorption |
| RISK-09 | Adoption fails because the desk keeps using its spreadsheet | Medium | High — the product's value is zero if unopened | G6's time-and-motion; login and disposition telemetry in pilot | Digests bring the queue to the user; case management makes it the system of record; G4's usability gate; the memo replaces a document the desk already produces by hand | Treat it as a product defect, not a training problem: find the step that is slower than the spreadsheet and fix it |
| RISK-10 | Key-person dependency on a single agent-driven build | Medium | Medium | Review backlog; undocumented decisions | Every decision recorded as an architecture decision record; tests encode intent; the task documents are self-contained; a human reviewer approves every merge | Onboarding cost is a week, not a rewrite, because the specification and tests carry the intent |

---

## §27 Open questions and decisions

| ID | Question | Owner | Blocked until answered | Default if unanswered |
|---|---|---|---|---|
| [OPEN-01] | Does any granted or pending patent claim covenant-breach-horizon prediction? | Legal | Commercial launch only, not engineering | Claim combination novelty only; assert no patent position |
| [OPEN-02] | Model provider commercial envelope: rate limits, quota, context window, latency, token accounting, price | Customer / provider | The published per-borrower cost figure; nothing structural | Assume tight: small prompts, ceilings on, cost logged and surfaced, budget alerting on |
| [OPEN-03] | Which identity provider and protocol will the customer use, and can we obtain test metadata? | Customer IT | SSO configuration only | Ship with the local store; SSO configured at deployment without a code change |
| [OPEN-04] | What are the customer's core-banking and LOS extract layouts, and can we get a masked sample? | Customer IT | The customer-specific connector mapping | Build against the documented generic layouts; map at deployment; the mapping is configuration |
| [OPEN-05] | Which news and industry feed will be licensed? | Customer procurement | That evidence family's live source | Synthetic generator supplies it; the adapter is built and tested against a recorded fixture |
| [OPEN-06] | What calibrates the thresholds and forecast weights for this customer's book? | Risk head, in pilot | Calibrated probabilities and G1/G3 on real accounts | Ship documented defaults calibrated on the reference portfolio, labelled as such, with the calibration procedure documented |
| [OPEN-07] | SMTP relay, sender domain and webhook endpoints | Customer IT | Notification delivery | Queue and surface in-app; configure at deployment |
| [OPEN-08] | Penetration test vendor and slot | Security | Release sign-off (N-04.g) | Release cannot proceed; this is a hard gate |
| [OPEN-09] | Retention periods the customer's policy requires, per class | Customer compliance | The configured retention schedule | Regulatory minimums as documented in §21 |
| [OPEN-10] | Does the customer require the statistical stage-4 challenger, or is the deterministic forecaster sufficient? | Risk head | Nothing — the challenger is off by default | Deterministic forecaster is the champion; the challenger stays unpromoted |

**Decisions already made, with what would change them.**

- **Deterministic forecasting as the shipped default, with ML behind governance (§3.4).** Chosen for traceability, auditability and the regulator's explainability expectation, taking the accuracy-versus-traceability trade deliberately toward traceability. *Would change if:* a challenger beats the champion on the harness on a real portfolio and a named risk owner approves promotion — which is exactly the path N-12 builds.
- **Single-host deployment (§11.1).** Chosen because the customer operates it, the data is sensitive, the portfolio sizes are within one host, and every distributed component is an operational burden the customer would carry. *Would change if:* a customer exceeds the capacity model; §13.4's ordered list is the answer.
- **PostgreSQL for production, SQLite for development (§13.1).** Chosen for real constraints and concurrency in production and zero-friction offline development and evaluation. *Would change if:* a customer standard mandates another engine; the data-access layer is the abstraction that makes it possible, at the cost of the Postgres-specific features named in §13.
- **Server-rendered UI with HTMX rather than a single-page application (§13.1).** Chosen because authorization stays on the server, the design system is a document idiom, there is no build chain to secure or ship, and the product must work on a locked-down desktop. *Would change if:* a capability genuinely needs client-side state beyond what HTMX carries; the horizon control was the hardest case and it did not.
- **Advisory-only, human-decisioning posture (P-01).** Chosen because the regulation requires it and because the product's credibility rests on it. *Nothing changes this.*
- **A pluggable model provider rather than a fixed one (§13.1).** Chosen because the Outsourcing Direction requires an exit strategy and an architecture is a better exit than a promise. *Nothing changes this.*
- **One coding agent at a time (§11.2).** Chosen because the alternative — two agents on one codebase — needs file-ownership fences, merge coordination and disjointness proofs that cost more than the parallelism returns at this size. *Would change if:* the team grows to multiple human engineers, at which point the ordering in §28 is already the parallelisation plan.

---

## §28 Delivery plan

### 28.1 Effort

Effort is in **ideal engineer-days**: one focused day of a competent engineer working with an AI coding assistant, including that work's own tests and documentation. It is not a calendar promise. Calendar duration depends on review throughput, environment availability and the customer dependencies in §12.1, and is agreed with the customer against these numbers rather than asserted here.

| Line | Ideal days |
|---|---|
| Functional requirements R-01…R-36 | **167** |
| Non-functional requirements N-01…N-12 | **55** |
| Project setup, tooling and CI pipeline | 5 |
| Design system: tokens, components, layout shell | 6 |
| Application shell: routing, auth wiring, error handling, i18n scaffolding | 4 |
| Reference portfolio generator and test fixtures | 4 |
| Evaluation example authoring (content, not harness) | 4 |
| Threshold calibration on the reference portfolio | 3 |
| Integration and stabilisation between milestones | 12 |
| Release engineering, documentation polish, evaluation support | 8 |
| Penetration-test remediation allowance | 4 |
| **Subtotal** | **272** |
| Contingency at 20% | 55 |
| **Total to 1.0 GA** | **327** |

The contingency is not slack to be spent on optional work; it absorbs what nobody thought of, and it is reported against rather than quietly consumed. Unlike the 0.1 draft, whose own checks recorded a reserve of 0.7 hours against a 9-hour floor, this plan is funded above its floor and has no cut list standing by to rescue it.

### 28.2 Milestones

Each milestone ends at a gate with objective exit criteria. A milestone that runs long moves the following ones; it never drops a capability. Every milestone leaves the system installable, migratable and green.

| # | Milestone | Contents | Days | Exit gate |
|---|---|---|---|---|
| **M-0** | **Foundation** | Project structure, tooling, CI, quality gates · R-01 data model and migrations · R-02 master data · N-03 configuration and thresholds · N-04 security core (auth, RBAC, scoping, crypto, secrets) · design system tokens and components · application shell | **34** | A user signs in, sees an empty scoped portfolio, and every quality gate is green on a clean install. Authorization tested per role per endpoint. Migrations upgrade and roll back |
| **M-1** | **Covenant core** | R-03 statement ingestion · R-07 ratio library · R-05 registry · R-08 engine · R-09 certificate workflow · reference portfolio generator | **29** | A covenant registered by hand tests correctly against imported statements, with exceptions, waivers, cure periods, staleness and not-computable all behaving as specified, and every hand-worked case exact |
| **M-2** | **Intelligence** | R-10 signal ingestion · R-11 evidence ledger · R-12 forecast · R-13 attribution · R-14 triage · R-15 simulation · threshold calibration | **35** | On the reference portfolio, the deteriorating cohort is dated within tolerance, the noisy cohort never escalates, the stable cohort stays below amber, drivers sum, and simulation deltas reconcile |
| **M-3** | **Interface and explainability** | R-20 audit and reconstruction · R-21 why-panel · R-22 queue · R-23 case file and horizon control · R-25 audit and governance screens · R-36 theming · N-07 accessibility | **29** | The primary flow completes end to end in the UI, by keyboard, in both themes; every figure resolves through the why-panel; a warning reconstructs and exports |
| **M-4** | **Documents, intake and memo** | R-04 document ingestion and OCR · R-06 verified intake · R-24 intake screen · R-16 action catalogue · R-17 memo and export · N-01 evaluation harness · N-12 model governance · evaluation examples | **39** | A sanction letter becomes live covenants through verification and approval; a memo generates, grounds, exports and refuses correctly; both harness arms score with gates enforced |
| **M-5** | **Workflow, integration and platform** | R-18 cases · R-19 overrides and dispositions · R-26 admin console · R-27 notifications · R-28 batch orchestration · R-29 connectors · R-30 feeds · R-31 regulatory reporting · R-32 API · R-33 search · R-34 bulk operations · R-35 languages | **51** | An overnight batch scores the reference portfolio, raises cases, dispatches digests and lands within its window; a file-drop connector reconciles; the API passes its contract tests; Hindi renders complete |
| **M-6** | **Hardening and release** | N-02 observability and SLOs · N-05 performance · N-06 backup and recovery · N-08 installer, upgrade, rollback · N-09 full test strategy · N-10 documentation · N-11 compliance pack · integration and stabilisation · release engineering · penetration-test remediation | **55** | §23's ten acceptance criteria, all true, on a clean install of the release candidate |

### 28.3 Working rhythm

One agent works one task at a time from the ordered backlog in `tasks.md`. Every task ends with its own tests passing and the full quality gate green. A human reviews and approves every change before it lands on the main line; nothing merges unreviewed. Milestone gates are demonstrated, not asserted: someone runs the exit criteria and records the result. Architecture decisions are captured as records at the moment they are made, because a decision reconstructed six weeks later is a guess.

---

## §29 Change history

| Date | Version | What changed | Who |
|---|---|---|---|
| 2026-08-29 | 0.1 | Initial specification written for a 24-hour hackathon: 11 functional and 5 non-functional `must` requirements, 1 `should`, 1 `later`, 9 `NG-` exclusions, a 4-rung cut ladder and 3 narrower feature variants, against a 60-working-hour budget | Spec lead |
| 2026-08-30 | **1.0** | **Rebuilt as a production product specification.** All nine `NG-` exclusions promoted to funded requirements (authentication and authorization, core-banking and LOS integration, external feeds, alert delivery, PDF and OCR ingestion, model retraining governance, multi-language interface, theming, and the removal of the no-real-feeds limitation). The `should` (what-if preview) and `later` (nightly batch) requirements promoted to `must` as R-15 and R-28. The cut ladder, the narrower feature versions and every `LATER:` deferral deleted. Requirement set expanded from 16 to 48. Deployment target changed from a demonstration laptop to a hardened single-host customer installation; stack changed to FastAPI, PostgreSQL, SQLAlchemy and Alembic with a pluggable model provider; hackathon clock, demo script and judging criteria replaced by milestones, customer pilot and acceptance criteria. Exclusions reduced to five permanent product-posture statements | Product and engineering |

Subsequent changes to this document require a version increment, an entry in this table and, where they change scope or cost, an agreed change record with the customer (RISK-08).

---

## §30 Requirements traceability

| `problem.md` obligation | Requirement | Verified by | Milestone |
|---|---|---|---|
| Ingest borrower financial statements, calculate ratios | R-03, R-07 | R-03.a–e, R-07.a–e | M-1 |
| Extract and represent covenant definitions, thresholds, testing frequency, exceptions | R-04, R-05, R-06 | R-04.a–e, R-05.a–e, R-06.a–e | M-1, M-4 |
| Monitor account activity, payment behaviour, facility utilization | R-10, R-29 | R-10.a–e, R-29.a–e | M-2, M-5 |
| Evaluate treasury flows and cash-movement changes | R-10, R-11 | R-10.a, R-11.a–e | M-2 |
| Incorporate concentration exposure and industry/news deterioration signals | R-10, R-30 | R-10.a, R-30.a–e | M-2, M-5 |
| Predict covenant breach probability at 30, 60 and 90 days | R-12 | R-12.a–f | M-2 |
| Identify the primary drivers of forecast risk | R-13, R-21 | R-13.a–c, R-21.a–d | M-2, M-3 |
| Rank borrowers/facilities by urgency and expected impact | R-14 | R-14.a–d | M-2 |
| Recommend RM, credit or risk interventions | R-15, R-16, R-17 | R-15.a–d, R-16.a–c, R-17.a–e | M-2, M-4 |
| Generate an auditable warning trail of data, trends, calculations and reasoning | R-20, R-21 | R-20.a–d, R-21.a–d | M-3 |
| Keep the contractual condition distinct from behavioural indicators | R-05, R-08, R-11 | R-05.a, R-08.a–f, R-11.a–e | M-1, M-2 |
| Update the risk view without losing evidence or historical trend | R-11, R-19, R-20 | R-11.c, R-19.a, R-20.a | M-2, M-3 |
| Make confidence and materiality visible | R-11, R-12 | R-11.d, R-12.d | M-2 |
| Avoid escalation on weak or transient evidence | R-11, N-01 | R-11.a, N-01.a | M-2, M-4 |
| Allow the risk view to be revised by later evidence | R-11, R-19 | R-11.c, R-19.a | M-2, M-5 |
| Keep recommendations advisory with human review | P-01, R-16, R-17 | §16.1 matrix, R-16.a, R-17.a | M-4 |
| Reconstruct why a warning was raised, what supported it, how it changed | R-20, R-21 | R-20.a–d, R-21.a–d | M-3 |
| Executable locally with no live core-banking integration required | §11.1, R-28, R-29 | N-08.a, R-28.a, R-29.a | M-5, M-6 |
| Synthetic portfolios for development and evaluation | Reference portfolio, N-01 | N-01.a–d, §22.1 | M-1, M-4 |

Every obligation in `problem.md` is carried by at least one funded requirement with an executable check and a milestone. No obligation is answered by an exclusion.

---

## §31 Self-checks on this document

1. **OK** — no capability named in `problem.md` is excluded, deferred, marked `later`, or given a reduced variant. §30's traceability table has no empty cell, and §8.2's five exclusions are product posture (advisory-only, no fraud classification, no origination, no rating, no autonomous account action), each justified by regulation or product identity rather than by schedule.
2. **OK** — the deferral marker `LATER:` is used nowhere as a deferral in this document; the only occurrences are in §29's change history and in this line, both describing its removal. No requirement is priced at zero. The 0.1 draft's four-rung cut ladder and three narrower feature versions are deleted, and §8.3 states that a scope conversation is a customer change record rather than a pre-authorised removal.
3. **OK** — every requirement carries a why, a behaviour and at least one failure-path check. Every check names an observable outcome rather than an intention.
4. **OK** — §28's arithmetic sums: 167 functional + 55 non-functional + 50 supporting = 272 subtotal; 20% contingency 55; total 327. The milestone table's day column sums independently to 272, matching the subtotal before contingency, and `tasks.md`'s 173 task blocks sum to the same 272 with each milestone matching exactly.
5. **OK** — the execution model is a single agent throughout. No lane, batch, disjointness test, assistant-capacity window or cross-agent merge protocol appears in this document, and §11.2 states the constraint explicitly.
6. **OK** — every regulatory obligation in §2.1 maps to a requirement and a check, and §16.4 lists where an auditor is shown each one.
7. **OK** — the model's authority is bounded in §17.1 by construction rather than instruction: the domain layer cannot import the provider, figures are injected into slots, and a numeric token outside a slot refuses the memo. Stages 2 through 6 are code, and §17.6's why-panel opens all of them.
8. **OK** — every threshold in §17.5 names its boundary behaviour, lives in configuration, requires approval, and is snapshotted and referenced by the records that used it.
9. **OK** — §15's direction, refusals, palette, type, spacing, motion and composition rules are stated concretely enough for a reviewer to hold a screen against them, and both themes carry contrast floors that §23 gates on.
10. **OK** — §18's targets each name a number and a measurement method, and §23 requires every one to be measured and published including misses.
11. **OK** — §19 covers empty, oversized, hostile, duplicate, malformed, stale, undefined, unavailable, over-budget, concurrent, expired and crashed cases, each with what the user is told and what must not be lost.
12. **OK** — §21's retention schedule covers every entity class in §14.2, and §11's erasure procedure preserves audit-chain validity rather than breaking it.
13. **OK** — §22 replaces the judged demo with an evaluation build, an eight-question challenge list, a two-quarter pilot with exit measurements, and the single weakest claim named before anyone finds it.
14. **OK** — §26's ten risks each carry an early-warning signal that maps to a metric or an alert in §20, and none is mitigated by removing a feature.
15. **OK** — §27's open questions each name an owner, what is blocked, and a default; only two ([OPEN-08] penetration test, and [OPEN-01] for commercial launch) are hard gates, and both are requested at M-0 rather than discovered at M-6.

*Two honest residuals.* First, §22.4's calibration claim stands until a pilot closes it, and no amount of internal testing substitutes for a real portfolio — this is stated in the product, not only in this document, because until calibration completes the interface leads with confidence and lead time rather than an uncalibrated probability. Second, the effort figures in §28 are estimates by an engineering judgement, not measurements; they are stated as ideal days precisely so that calendar commitments are negotiated against them rather than derived from them.

*(End of spec.md — version 1.0, 2026-08-30.)*

