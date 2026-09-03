# Business Requirements Document

## Covenant Radar — AI-Powered Dynamic Covenant Monitoring & Early Warning

**Domain:** Commercial Banking · Credit Risk
**Document status:** Derived from `problem.md` (business problem statement) and `spec.md` v1.0 GA (product specification), 2026-08-30.
**Prepared:** 2026-09-03

---

## 1. Purpose of this document

This Business Requirements Document (BRD) states, at business altitude, why Covenant Radar is being built, what business outcomes it must produce, who it serves, what is in and out of scope, and the business-level requirements the solution must satisfy. It is derived from two source documents:

- **`problem.md`** — the original business problem statement and required capabilities.
- **`spec.md`** — the full product specification (functional requirements `R-nn`, non-functional requirements `N-nn`, user flows `F-nn`, risks, milestones) that answers `problem.md` in engineering detail.

Where this BRD summarizes a requirement, the corresponding `spec.md` identifier is given in parentheses so the two documents can be cross-referenced. This BRD does not restate implementation detail (architecture, data model, API shapes) — see `spec.md` §13–§14 for that.

---

## 2. Business context and problem statement

### 2.1 The problem, plainly

Indian commercial banks monitor loan covenants primarily through **periodic borrower-submitted reporting** — quarterly QIS Forms, stock statements, MSOD returns — reviewed by hand. A borrower's financial position can deteriorate materially *between* these reporting cycles, while warning signs already exist in account activity, facility utilization, payment behaviour, treasury flows, industry conditions and concentration exposure. Because monitoring is quarterly and manual, deterioration is caught late — often only when a covenant has already been formally breached — at which point intervention options have narrowed and recovery value has fallen.

### 2.2 Why this matters, in numbers

| Finding | Figure | Business implication |
|---|---|---|
| Once an account reaches insolvency, recovery is poor | 67% average haircut on admitted claims (IBBI, to Sep 2025) | Every day of earlier warning has direct rupee value |
| Loan-related fraud is growing sharply | ₹33,148 crore in FY25, up 194% YoY | Behavioural signals (payments, treasury flows), not just financials, must be monitored |
| Wilful default has grown roughly tenfold in a decade | ₹3,83,264 crore outstanding (Mar 2025) | Borrower-behaviour evidence is as important as ratio arithmetic |
| Behavioural stress (SMA-2) is a documented leading indicator of NPA slippage | Precedes slippage by ~2 quarters (RBI FSR) | The signals needed to warn early already exist; today's process discards them between reporting cycles |
| Legacy early-warning systems drown analysts in false positives | ~95% false-positive rates reported in transaction monitoring | Any solution must control noise as rigorously as it detects signal, or it will be ignored |

Gross NPAs across scheduled commercial banks are currently at a multi-decadal low (2.3% Mar 2025). The business case for Covenant Radar is therefore **prevention of future deterioration**, not cleanup of an existing crisis — earliness converts directly into avoided haircut and preserved optionality for relationship, credit and risk teams.

### 2.3 The business challenge

The system must distinguish **meaningful, sustained deterioration from temporary noise**, and must provide **evidence-backed early warning** — not simply flag a covenant after it has already been breached. Doing this credibly, at a bank a regulator will inspect, additionally requires that every number the system shows be **traceable back to its source and reconstructable**, because Covenant Radar's outputs feed processes (Early Warning Systems, Red-Flagged-Account referral) that RBI's Fraud Risk Management Directions govern.

---

## 3. Business objectives and success metrics

The solution is judged against seven measurable business goals (`spec.md` §6). Two — earliness and noise control — are a deliberate pair, because either alone is trivially gamed by moving a threshold.

| # | Business goal | Target | Why it proves the business case |
|---|---|---|---|
| **G1** | Warn before the breach, not after | ≥70% of deteriorating borrowers flagged ≥30 days ahead of their covenant test date; ≥50% flagged ≥60 days ahead | Directly measures the earliness `problem.md` asks for |
| **G2** | Convert earliness into recovered value | ₹ value = exposure × recovery-point gain, tracked per intervention in pilot | Ties the product to the 67% haircut anchor — earliness has to show up as rupees, not just alerts |
| **G3** | Do not drown the desk with false alarms | ≤10% of portfolio in amber-or-worse on any scoring day; false-escalation rate ≤5%; ≥60% of escalations acted on | Directly answers the "core challenge" in `problem.md` — sustained deterioration vs. noise |
| **G4** | Usable without training | A credit officer completes the primary flow unaided in ≤3 minutes, ≤2 stalls | The product only creates value if the desk can use it on day one |
| **G5** | Every shown figure is defensible | 100% of sampled on-screen figures resolve to a source record; an evidence bundle exports in one action | This is what makes the product safe to put in front of an RBI inspector |
| **G6** | Monitoring effort per account falls | ≥60% reduction in analyst-minutes per account per quarter vs. manual QIS review | The product must return hours to the desk, not add work |
| **G7** | It stays up and stays fast | 99.5% monthly availability in business hours; overnight batch completes before 07:00 IST on ≥99% of nights | A monitoring product that is down or late on the morning queue has failed its job |

**Why not the easy metrics.** Alerts raised, borrowers scanned or dashboards viewed would all look impressive from a system that has failed — a system that alerts on everything maximizes all three. G1 and G3 are load-bearing precisely because they cannot both be gamed by the same lever.

---

## 4. Business capabilities required

`problem.md` states nine required capability areas; `spec.md` groups these into six purchasable capability pillars. The mapping below is the core of this BRD — it is the business requirement, independent of how it is engineered.

### 4.1 Capability pillars

**Pillar 1 — Turn a sanction letter into a live, trustworthy covenant record.**
A credit officer uploads a sanction letter, amendment letter, or compliance certificate. AI proposes the structured covenant fields (definition, threshold, frequency, exceptions); the system then **independently recomputes and verifies every proposed field in code** before a human can confirm it — a wrong proposal is refused, never silently registered. Registered covenants are versioned and become immutable once tested.
*Business need it answers:* "Extract and represent covenant definitions, thresholds, testing frequency and exceptions" (`problem.md`).

**Pillar 2 — A 30/60/90-day breach radar with a movable date.**
For every borrower, the system shows which covenant is likely to break first, on what **projected date**, at what probability and confidence, and what is driving that risk. A horizon control lets a user walk time forward and watch headroom deplete; an intervention simulator shows what a specific action (e.g., a limit reduction) would do to that date.
*Business need it answers:* "Predict covenant breach 30/60/90 days in advance" and "Identify the primary drivers contributing to forecast risk" (`problem.md`).

**Pillar 3 — Committee-ready memos and a reconstructable audit trail.**
An intervention memo whose every figure can be traced back to the record that produced it, exportable for a credit committee pack; overrides are logged with mandatory reasons; any past warning can be fully reconstructed — source data, calculations, evidence, thresholds, forecast and recommendation — for an internal reviewer or an external inspector.
*Business need it answers:* "Generate an auditable warning trail showing data, trends, calculations, and reasoning" (`problem.md`).

**Pillar 4 — Runs the desk, not just the screen.**
Overnight batch scoring so the day's queue is ready before anyone logs in; email/webhook digests; SLA-tracked cases with assignment and status; saved views, search and bulk export. The distinction between a report nobody opens and a system of record for the monitoring process.

**Pillar 5 — Plugs into the bank it is installed in.**
Connectors into core-banking, loan-origination and treasury systems (read-only); external feed adapters for industry, news and bureau signals with entity resolution; regulatory exports (CRILC-format, EWS/RFA pack, board MIS); a documented API.

**Pillar 6 — Can be run, audited and defended by the bank's own people.**
Bank SSO or local login, role-based permissions enforced server-side, maker-checker on covenant registration and threshold changes, encryption of personal data, structured logging with SLOs, backup/restore, an installer, upgrade/rollback, and a compliance evidence pack.

### 4.2 Coverage against `problem.md`

Every capability named in the original problem statement is answered, with no exclusion required to escape it:

| `problem.md` requirement | Answered by |
|---|---|
| Monitor covenant thresholds together with financial and behavioural signals | Covenant engine + evidence ledger |
| Forecast breach risk at 30/60/90-day horizons | Horizon forecast |
| Explain the drivers of deterioration | Driver attribution + why-panel |
| Recommend prioritized interventions | Simulation + action catalogue + memo |
| Distinguish meaningful deterioration from noise (the core challenge) | Evidence ledger's persistence/materiality scoring |
| Ingest financial statements and calculate ratios | Statement ingestion + ratio library |
| Extract and represent covenant definitions, thresholds, frequency, exceptions | Verified covenant intake + registry |
| Monitor account activity, payments, utilization | Signal ingestion + connectors |
| Evaluate treasury flows and cash-pattern changes | Treasury evidence type |
| Concentration exposure and industry/news deterioration signals | Evidence types over real and synthetic feeds |
| Rank borrowers/facilities by urgency and impact | Portfolio triage |
| Recommend RM/credit/risk interventions, advisory and human-reviewed | Memo + action catalogue + posture rule (§5.2) |
| Auditable warning trail | Audit trail, reconstruction and why-panel |
| Keep covenant condition distinct from behavioural/external indicators | Registry and evidence ledger are separate records and screens |
| Risk view revisable as evidence changes | Evidence supersession, never deletion |
| Confidence and materiality visible | Shown on every forecast and evidence item |
| Executable locally, no live core-banking integration required | Runs on one host; connectors are optional |

---

## 5. Scope

### 5.1 In scope for the 1.0 release

There is no phased "later" list — everything below is funded for general availability:

- **Data and ingestion:** borrower/facility/portfolio master data; financial statement ingestion (CSV, XLSX, JSON, API) with validation and restatement support; document ingestion with OCR and source-span provenance; account-conduct and transaction signals; core-banking/LOS/treasury connectors (read-only); external industry/news/bureau feeds; a seeded demonstration portfolio.
- **Covenant management:** versioned covenant registry; a 24-ratio library plus validated custom formulas; exceptions, waivers, cure and grace periods; AI-assisted, code-verified intake with maker-checker; compliance certificate workflow.
- **Intelligence:** deterministic covenant engine with SMA banding; evidence ledger across six signal families; 30/60/90-day and arbitrary-horizon forecasting with dated crossing; driver attribution; intervention simulation; portfolio triage and ranking.
- **Workflow and output:** case management; grounded intervention memos (PDF/DOCX export); override and disposition capture; configurable action catalogue; notifications and digests; scheduled batch scoring; regulatory exports (CRILC, EWS/RFA, board MIS); search, saved views, bulk export.
- **Interface:** portfolio queue, borrower case file with horizon control, covenant intake and review, intervention simulator, memo composer, audit and reconstruction, governance/model-oversight views, admin console — in English and Hindi, light and dark themes, accessible to WCAG 2.2 AA.
- **Platform:** authentication (local + bank SSO), server-enforced role-based access with row-level scoping, maker-checker, encryption of personal data, an append-only audit store, observability and SLOs, versioned migrations, backup/restore, installer/upgrade/rollback, a public REST API, an evaluation harness with a baseline comparison arm, and a model governance registry.

### 5.2 Permanently out of scope — product posture, not a deferral

These five exclusions are deliberate and permanent — required because a regulator, not a project schedule, says so:

- **No automated credit decisions, waivers or escalations.** Every output is advisory; a human reviews and decides. No configuration flag changes this.
- **No fraud classification.** Radar surfaces signals a Red-Flagged-Account process consumes; declaring fraud is a separate, regulated human act.
- **No loan origination, underwriting or sanction-time credit scoring.** Radar begins after sanction.
- **No credit rating.** Radar produces covenant breach risk on a named contract, not an entity-level rating.
- **No autonomous action on borrower accounts.** Radar never places a hold, changes a limit, blocks a drawdown or initiates a payment; connectors are read-only, by design and in the codebase.

### 5.3 Also explicitly not this product

Retail/MSME collections; a document-management system (though documents are retained); market, liquidity or operational risk monitoring; IFRS-9/ECL provisioning computation (Radar feeds provisioning inputs, does not compute the provision); a general chat assistant over bank policy.

### 5.4 Sequencing

There is no cut list. If commercial reality later forces a scope conversation, it happens against the milestone boundaries in §9 of this document, is recorded as a formal change, and is never a silent scope reduction.

---

## 6. Stakeholders and roles

Access and authority are enforced by the system itself (role-based, server-side), not left to convention:

| Role | Primary need | May do | May never do |
|---|---|---|---|
| **Relationship Manager** | An early, specific, evidence-backed heads-up on their accounts | View assigned portfolio, read/export memos, record actions, comment | Change covenants/thresholds/forecasts; see accounts outside assignment |
| **Credit Officer** | Covenant intake without re-keying, trustworthy because code re-verifies it | Upload documents, review/correct AI proposals, register covenants, record waivers | Confirm a covenant that failed verification |
| **Credit Approver** | Assurance that what was registered matches the sanction letter | Approve or reject a pending registration/amendment/waiver | Register and approve the same record (maker-checker) |
| **Risk Officer** | A noise-controlled, ranked watchlist and the power to disagree | Generate memos, run simulations, override with a reason, manage watchlist | Override silently — every override is logged with a mandatory reason |
| **Risk Head / Approver** | Portfolio oversight and model governance | Approve threshold changes and model promotions; sign monthly SLO/drift reports | Edit or delete an audit event |
| **Auditor / Inspector** | Full reconstruction of any warning | Read everything, including the audit trail; export an evidence bundle | Change anything |
| **Administrator** | A system that runs, users who can log in, connectors that reconcile | Manage users, roles, connectors, notification channels, action catalogue | Read personal data in the clear without a recorded access purpose |
| **Data Steward** | Correct, reconciled, provenance-tracked data | Resolve entity-matching conflicts, correct a mis-parsed statement | Alter a covenant test result directly |

**Language and locale:** English is the default; Hindi ships as a second language because not all branch-level desk users are comfortable in English. Currency, dates and quarters follow Indian conventions (₹, lakh/crore, IST, Indian financial-year quarters) throughout.

---

## 7. Regulatory drivers

This product exists inside — and must satisfy — a specific Indian banking regulatory framework. These are business requirements, not optional compliance nice-to-haves:

| Regulation | What it requires of the business | How the product answers it |
|---|---|---|
| **RBI Master Directions on Fraud Risk Management (Jul 2024)** | Board-approved Early Warning System integrated with core banking; Red-Flagged-Account reporting to CRILC within 7 days | Auditable, reconstructable warning trail; core-banking connectors; EWS/RFA export pack; advisory-only posture |
| **RBI Prudential Framework for Resolution of Stressed Assets (Jun 2019)** | SMA-0/1/2 banding by days overdue; CRILC monthly/weekly reporting | Native SMA banding; CRILC-format export; 30-day review reminders |
| **Digital Personal Data Protection Act, 2023** | Purpose limitation, access control, retention limits, erasure rights for guarantors/promoters/directors named in loan files | Field-level encryption, retention schedule with automated enforcement, documented erasure procedure |
| **RBI FREE-AI Committee report (Aug 2025)** | Explainability, accountability and fairness for AI used in financial decisions; board-approved AI policy | A "why" panel over every stage of every decision; model registry, drift monitoring, rollback governance |
| **RBI Master Direction on Outsourcing of IT Services (Apr 2023)** | Minimum-necessary data to any external processor; audit rights; an exit strategy from any vendor | Outbound data whitelisting/masking to the AI provider; full call logging; a pluggable provider architecture as the exit strategy |
| **CERT-In Directions (Apr 2022)** | Cyber incidents reported within 6 hours; logs retained 180 days in-country | 180-day log retention with integrity hashing; documented incident runbook |

**The one open regulatory item:** a freedom-to-operate patent search (to confirm no prior claim on covenant-breach-horizon prediction) is a gate on **commercial launch**, not on engineering delivery.

---

## 8. Business assumptions and dependencies

### 8.1 Assumptions this BRD relies on

- Corporate credit documentation and covenant text are in English.
- The customer's exposures of interest are ≥ ₹5 crore (the CRILC reporting band), with a portfolio in the tens of thousands of facilities or fewer.
- Financial statements arrive at least quarterly and account-conduct data at least daily.
- The customer accepts an advisory-only tool with human decisioning — this is contractual, not negotiable.
- The customer will supply a sample cohort of accounts with known outcomes for pilot calibration; without it, the system ships with documented default thresholds calibrated on a synthetic reference portfolio.

### 8.2 What the business must supply, and what happens if it does not

| Dependency | Owner | If it is late or unavailable |
|---|---|---|
| AI provider access and its commercial terms (rate limits, cost) | Customer / provider | The covenant-intake and memo-writing steps degrade to manual entry and a template memo; the core monitoring, testing and forecasting mechanism is unaffected |
| Bank identity provider details for SSO | Customer IT | The system ships with its own local login; SSO can be added later without a rebuild |
| Core-banking / loan-origination extract specifications and a sample file | Customer IT | Built against documented generic layouts; the bank-specific mapping is configuration done at deployment |
| A licensed news/industry data feed | Customer procurement | A synthetic signal generator supplies that evidence type for development, testing and demonstration |
| Historical portfolio data for calibration | Customer | Thresholds ship at documented, synthetic-portfolio-calibrated defaults; earliness and noise-control metrics remain lab-measured until a live pilot |
| A penetration test slot | Security vendor | Blocks final release sign-off — this is a hard gate, not a soft one |

---

## 9. Delivery approach (business view)

The full specification breaks delivery into six milestones, each ending at an objectively demonstrated gate (not an assertion) — foundation and security core; covenant registry and engine; forecasting and simulation intelligence; the user-facing interface and explainability; document intake and memo generation; and finally workflow, integrations and platform hardening leading to release. A milestone that runs long moves the release date; it does not drop a capability, because there is no scope reserve set aside for that purpose (see §5.4).

Full effort estimates, milestone-by-milestone content and exit criteria are in `spec.md` §28.

---

## 10. Definition of business success

The release is considered business-ready only when, in addition to all engineering checks passing, the following are demonstrably true (`spec.md` §23, restated at business level):

1. Every stated capability has been checked and verified, with no unaddressed failure.
2. The system's forecasting is scored against an honest, naive baseline on every release, and the comparison is published — the product must prove it beats the simple alternative, not merely exist.
3. An independent security review (penetration test) is complete with no unresolved high or critical finding.
4. Every regulatory obligation in §7 maps to a specific control and a passing test.
5. A clean installation, a data backup/restore, and an upgrade/rollback have all been rehearsed and timed on a fresh host by someone who did not build the installer.
6. The interface has been accessibility-audited (WCAG 2.2 AA) and usability-tested with real desk users who can complete the primary flow unaided.
7. Administrator guide, user guides, API reference, operations runbook and compliance evidence pack are complete and independently reviewed.

---

## 11. Key business risks

| Risk | Business impact if it occurs | How it is managed |
|---|---|---|
| Forecast probabilities are not trusted by domain experts because they are calibrated on synthetic data until a live pilot | Undermines the headline value proposition | The deterministic engine and dated headroom (pure arithmetic) are presented as the credible core; probability is a ranking aid pending pilot calibration |
| Noise control fails and the product escalates too much | Recreates the alert-fatigue problem this product exists to solve; kills adoption | Persistence/materiality thresholds are tuned against a paired earliness-and-quiet goal on every release, not earliness alone |
| Customer data quality is worse than assumed (missing quarters, no daily conduct feed) | Degrades every downstream forecast | Confidence is always shown and falls visibly rather than silently; data quality issues are a joint remediation workstream, not a hidden failure |
| The desk keeps using its existing spreadsheet instead of adopting the tool | The product's value is zero if unopened | Digests actively push the queue to users; the product must be measurably faster than the spreadsheet it replaces, or the gap is treated as a product defect |
| Scope grows during pilot as the customer requests bespoke reports or connectors | Budget and timeline pressure | Connectors, feeds, catalogues and reports are built as configuration wherever possible; anything beyond that is a formal, costed change to this specification |

Full risk register with likelihood, impact and early-warning indicators: `spec.md` §26.

---

## 12. Cross-reference index

| Business topic | `spec.md` section |
|---|---|
| Business goals and metrics | §6 |
| User roles and permissions | §7 |
| In/out of scope | §8 |
| End-to-end user journeys | §9 |
| Full functional and non-functional requirements | §10 |
| Regulatory obligations | §2.1 |
| Assumptions and dependencies | §12 |
| Risks | §26 |
| Open questions | §27 |
| Delivery milestones | §28 |
| Release acceptance criteria | §23 |
