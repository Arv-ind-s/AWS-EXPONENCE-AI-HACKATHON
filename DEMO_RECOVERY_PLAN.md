# Complete UI, Workflow, OCR, ML, and Demo Recovery Plan

## Summary

Make every specification-backed browser feature complete and demonstrably functional without breaking working domain behavior. The implementation begins with a route/control contract and regression baseline, fixes broken or missing workflows subsystem-by-subsystem, then proves every enabled control using role-aware Playwright tests.

Additional UI investigation confirmed:

- `/financial-statements` is linked from global navigation and the borrower workspace but no matching browser route exists.
- Queue row selection has only “Clear selection”; the existing bulk-operation/export services are not exposed through the selection bar.
- Saved-view APIs exist and queue loading accepts `view_id`, but users have no complete create/edit/share/default/delete workflow in the UI.
- English/Hindi rendering support exists internally, but there is no visible language switcher.
- Connector, feed, and entity-match capabilities lack the complete administration workflow required by `spec.md`.
- Admin users displays API-key counts without providing the corresponding key-management workflow.
- Regulatory/RFA export templates and services exist, but there is no complete judge-facing browser journey to produce and retrieve them.
- The catalogue screen is shown under `MANAGE_CONNECTORS`, although managing the intervention catalogue is not connector administration.
- “Log action” remains an intentional disabled placeholder despite the case workflow already supporting action logging.
- Document uploads are refused in the real application because production does not inject a virus scanner.
- OCR is disabled in the current environment.
- ML challenger results are operationally replacing deterministic probabilities without registry approval and are not identified in the UI.
- Existing browser tests cover selected components and screens, not activation and outcome verification for every rendered control.

## 1. Build the Regression and UI Contract Gate

### Protected baseline

- Preserve all currently passing deterministic calculations, security boundaries, API responses, audit behavior, and tests.
- Record characterization fixtures for queue ordering, forecasts, evidence, cases, notifications, traces, simulations, documents, OCR, memos, and governance.
- Restore the locked development/test environment, including scikit-learn, Hypothesis, import-linter, PostgreSQL, Playwright, PDFium, Tesseract, and the virus-scanner adapter.
- Use disposable databases and stores for interactive tests. Never run destructive control tests against the presenter database.
- Divide implementation into independently reversible subsystem batches. Do not mix domain changes, schema changes, styling rewrites, and demo-data repair in one batch.

### Machine-readable UI contract

Create a test-owned control manifest generated from rendered screens. Every control entry must identify:

- screen and stable control ID;
- visible label in English and Hindi;
- supported roles and required permission;
- prerequisite data/capability;
- method and route;
- service operation;
- CSRF/idempotency behavior;
- expected success result;
- expected validation, conflict, forbidden, unavailable, and retry result;
- no-JavaScript fallback;
- audit event and persisted state expected;
- Playwright test that proves the contract.

Fail CI for:

- enabled controls without a route or handler;
- forms whose action is not mounted;
- button-shaped disabled placeholders;
- controls with no observable result;
- unauthorized controls rendered as enabled;
- state-changing forms without CSRF;
- HTMX-only actions with no HTML fallback;
- duplicate labels without accessible names;
- untested controls;
- specification workflows with no browser entry point.

### Browser crawl

- Render every screen for every seeded role and every designed state.
- Activate every enabled button, link, menu item, form, filter, pagination control, drawer, tab, checkbox action, and keyboard shortcut.
- Assert one observable result: navigation, DOM replacement, drawer state, download, persisted mutation, validation message, queued job, or explicit capability error.
- Capture console errors, page errors, failed requests, CSP violations, missing assets, focus loss, and unexpected full-page/fragment responses.
- Repeat at 1440×900, 1366×768, 1024×768, 390×844, 320px/200% zoom, light/dark, English/Hindi, keyboard-only, and JavaScript-disabled modes.

## 2. Complete Every Specification Workflow in the UI

### Global shell and personal workspace

- Verify sidebar open/close/collapse, mobile scrim, search shortcut, search submission, live drawer, theme switch, user menu, password change, MFA, sign-out, and session-expiry recovery.
- Add an English/Hindi switcher that persists the locale securely and returns to the current scoped URL.
- Add accessible labels and visible feedback for theme/locale changes.
- Add recent-items and saved-view access to the shell or queue as required by R-33.
- Ensure live activity reconnects after temporary network loss, reports degraded/disconnected state, and never claims “Live” when polling has failed.

### Queue, saved views, bulk actions, and exports

- Make every queue filter round-trip through URL and HTMX with identical results.
- Fix urgency display without changing urgency calculation or ordering.
- Add working selection actions:
  - assign selected cases where permitted;
  - change human workflow state where allowed;
  - export selected rows;
  - generate the supported evidence/regulatory pack;
  - clear selection.
- Preserve per-row scope checks and report succeeded, excluded, conflicted, and failed items individually.
- Add create, rename, update, share, unshare, make default, remove default, and delete saved-view controls over the existing view service.
- Refresh or remove saved views safely when portfolio scope is narrowed.
- Complete asynchronous export status, retry, expiration, and download workflows.
- Restore selection after a harmless HTMX refresh where rows remain visible; clear excluded rows explicitly when the active run changes.

### Borrower, forecast, simulation, memo, and case workflows

- Make Why, simulation, memo generation, source-document links, evidence links, forecast horizon stops, overrides, dispositions, case assignment, state changes, comments, and action logging work from the borrower/case journey.
- Replace the disabled “Log action” placeholder with the existing case action form or a specific prerequisite link to create/open a case.
- Correct intervention applicability so the simulator always has valid options for the primary demo borrower.
- Keep simulation advisory, show assumptions before execution, compare with doing nothing, and persist the selected result.
- Generate the memo inline, focus and announce its completion, preserve it across reload, link every figure to its source, and expose PDF/DOCX export.
- Show clear queued, refused, unavailable, retry, duplicate-submit, and previous-valid-result states.
- Compose all seven Why stages from one coherent run without changing the exact-subject audit repository.
- Add stage-5 simulation and stage-6 triage traces.

### Documents, financial statements, OCR, and intake

- Add the missing `/financial-statements` browser screen and route over the existing financial-PDF/statement ingestion services.
- Support upload, mapping selection, dry-run preview, reconciliation, quarantine, correction/rejection, import confirmation, restatement, and provenance navigation.
- Inject a real Microsoft Defender scanner into production document composition; fail readiness and disable upload visibly if scanning is unavailable.
- Enable and configure Tesseract OCR for the demo.
- Move extraction/OCR into an idempotent processing job with pending, extracting, needs-review, complete, and failed states.
- Complete the browser workflow:
  1. upload;
  2. scan;
  3. native extraction/OCR;
  4. processing status;
  5. low-confidence review;
  6. corrected text with retained provenance;
  7. clause detection;
  8. model/hand proposal;
  9. code verification;
  10. human confirmation;
  11. covenant registration;
  12. source-span and stage-1 trace navigation.
- Exercise native, scanned, mixed, rotated, two-column, blank, encrypted, malformed, oversized, malicious, and instruction-injection PDFs.
- Never silently use low-confidence OCR text or partially persist failed extraction.

### Covenant, certificate, override, and disposition workflows

- Verify covenant create, amendment, retirement, waiver, exception, approval, rejection, duplicate/amendment choice, and effective-date behavior.
- Verify certificate request, acknowledgement, receipt, rejection, overdue state, document association, and notification.
- Ensure maker-checker controls never allow the proposing actor to approve.
- Make overrides require reason, expiry, and scoped subject; preserve original and overridden views.
- Make dispositions and explanation feedback visibly persist and appear in history.
- Remove component-gallery-only actions such as `/feedback` from production discovery or provide their real route when used on a product screen.

### Governance and model controls

- Restore deterministic champion/ML challenger separation.
- Display operational deterministic and shadow ML probabilities side by side with version, checksum, evaluation state, data origin, delta, and feature contributions.
- Keep queue, case, notification, simulation, and memo facts on the approved champion.
- Add model registration, evaluation review, maker-checker promotion, rejection, rollback, and drift-detail controls.
- Refuse promotion unless the registered artifact passes G1/G3, calibration, cohort, integrity, and approval gates.
- Display the current 14% false-escalation result as failed/not promotable, not as a successful customer model.
- Verify threshold proposal, preview, approval, rejection, version history, and configuration impact controls independently from model governance.

### Complete administration required by `spec.md`

Add browser workflows for capabilities currently available only as services/APIs or missing entirely:

- connector create/edit, field mapping, credential reference, dry run, reconciliation report, enable/disable, scheduling, run history, and retry;
- feed-source create/edit, quota/cost state, run, pause, and history;
- entity-match review, accept, reject, and negative-match memory;
- API-key create, one-time secret display, scope, expiry, revoke, and last-used state;
- user creation, role maker-checker, portfolio scope, session revoke, password reset, SSO mapping, deactivate/reactivate;
- intervention catalogue creation, approval, retirement, and applicability preview under the correct permission;
- job run/retry, recovered history, queue depth, health, and readiness details;
- retention preview/apply with immutable protected-record exclusions;
- model-provider capability/credential-reference rotation without displaying secrets;
- EWS/RFA/CRILC evidence-pack generation and download;
- audit search/export, reconstruction, evidence bundle, integrity verification, and missing-source reporting;
- data-principal erasure request, preview, approval, execution, and completion certificate where required by N-11.

The admin navigation must link to the correct screen for each permission. `MANAGE_CONNECTORS` must not be represented solely by the intervention catalogue.

## 3. Repair Shared Interaction and Failure Logic

- Standardize all state-changing forms with CSRF, optimistic version, duplicate-submit protection, audit correlation, and Post/Redirect/Get or correct HTMX fragment semantics.
- Use idempotency keys for actions that are not naturally idempotent.
- Preserve user input after validation, provider outage, conflict, or session renewal.
- Add confirmation for destructive actions and show the exact subject affected.
- Standardize loading, success, empty, degraded, validation, conflict, forbidden, queued, failed, and retry states.
- Every empty state must name the prerequisite and provide the permitted next action.
- Every unavailable state must name the missing capability and confirm what remains usable.
- Permission denial must be server-enforced; out-of-scope subjects remain `404`, not `403`.
- Fix HTMX CSP compatibility with nonce/hash configuration rather than `unsafe-inline`.
- Identify and fix every missing static asset; do not suppress console errors.
- Ensure normal HTML forms remain functional when JavaScript is disabled.
- Add transaction/concurrency tests for double-click, two-tab edit, stale versions, retry after timeout, browser back, reload during processing, and service restart.
- Keep advisory-only posture: no UI control may autonomously approve credit, grant a waiver, or make a credit decision.

## 4. Restore Demo Data and Judge-Facing Quality

- Correct demo covenant names, classes, units, thresholds, source documents, and intervention applicability.
- Build Act, Watch, and stable scenarios through production services.
- Give `B-000002`:
  - a real uploaded/OCR-processed document;
  - confirmed covenant proposals;
  - statement history;
  - covenant tests;
  - evidence;
  - deterministic forecast and ML challenger;
  - triage entry;
  - open assigned case;
  - notification and live activity;
  - applicable simulations;
  - complete seven-stage trace;
  - validated stored memo and exports.
- Reconcile missing notifications for existing eligible cases without creating duplicates.
- Retain historical job failures and label their successful recovery.
- Centralize Indian currency, percentage, ratio, date, quarter, and urgency formatting.
- Repair long-name/table layout and incrementally align screens with the ink-on-paper design.
- Do not remove the current styling layer wholesale until every dependent component has a tested replacement.
- Make bootstrap idempotent and add readiness checks for scanner, OCR, storage, model provider, ML artifact/registry, pipeline, notifications, simulations, traces, memos, and UI smoke results.

## 5. Acceptance Tests and Demo Gate

### Required automated scenarios

- Every control in the generated UI contract passes for every allowed role.
- Every forbidden role is denied server-side and does not receive an enabled control.
- Every mounted navigation link returns the expected screen; `/financial-statements` and all new administration routes are explicitly checked.
- Every form succeeds once, fails safely, preserves input, rejects stale/double submissions, and records its audit event.
- Saved views, bulk operations, exports, connectors, feeds, entity matches, API keys, regulatory packs, retention, model governance, and locale switching have real browser coverage.
- OCR completes from a real scanned PDF through covenant confirmation.
- ML challenger is visible but cannot change operational decisions without approved promotion.
- No console errors, page exceptions, CSP violations, missing assets, unexpected 4xx/5xx responses, broken links, or enabled no-op controls remain.
- WCAG 2.2 AA, keyboard, screen-reader structure, both themes, both languages, reduced motion, print, 320px, and 200% zoom pass.

### Judge walkthrough

1. Sign in and switch language/theme.
2. Use the queue filters, save the view, select borrowers, and run a bulk export.
3. Open `B-000002`; inspect deterministic and ML challenger forecasts.
4. Open the complete seven-stage Why chain.
5. Run and persist an intervention.
6. Log a case action, update assignment/state, and record a disposition.
7. Generate and export the grounded memo.
8. Upload a scanned PDF, review OCR, verify clauses, and confirm a covenant.
9. Import a financial statement and show reconciliation/provenance.
10. Show notification, live activity, and read state.
11. Show connector dry run, entity-match review, job recovery, governance scoreboard, and model promotion refusal.
12. Generate the evidence/RFA pack and verify its audit trail.

Demo readiness fails if any step, enabled control, dependency, or expected state is missing.

## Assumptions

- “Every feature” means every browser-relevant capability required by `spec.md` and `plan.md`; intentionally API-only R-32 endpoints retain API contract tests rather than redundant screens unless the admin console requires them.
- Existing deterministic calculations and public API response shapes remain protected.
- Schema additions are backward compatible and historical records remain readable.
- The deterministic forecast remains champion; the existing ML artifact remains a shadow challenger.
- Microsoft Defender, Tesseract, and PDFium are the Windows demo stack.
- Judge-visible records are produced through real services, not direct final-state fixtures.
- Historical audit, evidence, trace, document, and job records are not deleted to improve the demo.
