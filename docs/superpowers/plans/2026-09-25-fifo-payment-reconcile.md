# xe_fifo_payment_reconcile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Installable Odoo 17 addon that applies each released customer payment FIFO against the oldest open PUE invoices of the same commercial partner, one batch per cron run.

**Architecture:** New addon `xe_fifo_payment_reconcile` extending `res.partner` (opt-in flag) and `account.payment` (release toggle, state, `write()` hook, cron engine). One `ir.cron` (5 min backup) chained with `_trigger()`. Each batch runs inside a savepoint; state/chatter are written outside it.

**Tech Stack:** Odoo 17 Enterprise (`account`, `mail`, `l10n_mx_edi`), Python 3, psycopg2 error codes, `markupsafe.Markup`.

**Spec:** `docs/superpowers/specs/2026-09-25-fifo-payment-reconcile-design.md` (source of truth for every rule below).

## Global Constraints

- Branch `feature/17.0-fifo-payment-reconcile` from `main`; PR to `main`; never push to `main`.
- Manifest version `17.0.1.0.0`; depends `account`, `mail`, `l10n_mx_edi`.
- Code/strings in English, translated in `i18n/es.po`; code comments in Spanish (LATAM).
- Batch size param `xe_fifo_payment_reconcile.fifo_reconcile_batch_size`, default `250` (code and `noupdate="1"` XML).
- PUE only: payment method code `99` excluded as a fixed constant.
- No automated tests (manual testing in Rogelio's local dev branch). No data migrations. Do not touch `xe_meli_connector`.
- Group restriction is view-only (`groups=`); no group check in `write()`.
- Verified Odoo 17 anchors: `account.view_account_payment_form` → `group[@name='group2']`, `is_reconciled` already in form; `account.view_account_payment_tree` → `field[@name='state']`; `account.view_account_payment_search` → `filter[@name='reconciled']`; `account.view_partner_property_form` → `field[@name='property_account_payable_id']`; `ir.cron._trigger(at=None)`.
- Cron runs without `lang` in context: batch processing sets `lang` from the company partner so chatter is translated.

**Verification (no local Odoo):** each task ends with `python -m py_compile` on its `.py` files and an `lxml` parse of its XML files; final task runs a scratch script that checks every `_()` msgid in code has an `es.po` entry with matching placeholders.

---

### Task 1: Scaffold, partner flag and security group

**Files:**
- Create: `xe_fifo_payment_reconcile/__init__.py`, `__manifest__.py`, `models/__init__.py`, `models/res_partner.py`
- Create: `security/fifo_reconcile_security.xml`, `views/res_partner_views.xml`

**Produces:** `res.partner.fifo_auto_reconcile` (Boolean, tracking, copy=False); group xmlid `xe_fifo_payment_reconcile.group_fifo_reconcile_release` (no category, `noupdate="1"`).

- [ ] Write the files; partner field after `property_account_payable_id`.
- [ ] Verify: py_compile + lxml parse.
- [ ] Commit `feat(xe_fifo_payment_reconcile): add module scaffold, partner opt-in flag and release group`.

### Task 2: Payment fields, write() hook and views

**Files:**
- Create: `models/account_payment.py` (fields, `write()`, `_fifo_reconcile_trigger_cron(at=None)`)
- Create: `views/account_payment_views.xml` (form, tree, search)

**Produces:** fields `fifo_reconcile_release`, `fifo_reconcile_state` (`pending|in_progress|done|done_residual|error`), `fifo_partner_enabled` (related, not stored), `fifo_retry_count`; cron xmlid `xe_fifo_payment_reconcile.ir_cron_fifo_reconcile` referenced by the trigger helper (defined in Task 3; helper tolerates it missing).

- [ ] `write()`: when `vals` sets release True → add `state='pending'`, `retry_count=0`; after super, trigger cron.
- [ ] Form: toggle (`boolean_toggle`, `groups=`, invisible unless partner enabled, posted, not reconciled) and badge state with decorations; tree optional column after `state`; search filters after `reconciled` + group-by "FIFO State".
- [ ] Verify: py_compile + lxml parse.
- [ ] Commit `feat(xe_fifo_payment_reconcile): add payment release toggle, FIFO state and views`.

### Task 3: Reconcile engine, cron and batch-size parameter

**Files:**
- Modify: `models/account_payment.py` (engine methods)
- Create: `data/ir_cron.xml`, `data/ir_config_parameter.xml`

**Produces:** `_cron_fifo_reconcile()` (model method called by cron) → `_fifo_reconcile_run_batch()` → `_fifo_reconcile_apply_batch()` returning a result dict; `_fifo_reconcile_check_persisted(result)`; `_fifo_reconcile_handle_error(error, prefix=None)`; `_fifo_reconcile_handle_concurrency_error(error)`; `_fifo_reconcile_summary_html(result, elapsed)`; `_fifo_reconcile_invoice_domain(account)`; `_fifo_reconcile_batch_size()`.

- [ ] Implement per spec: currency check, single open pay line, candidate domain + FIFO order, lazy line-level traversal (`reconciled = False`, `date_maturity, id`), split reconcile, invariants after `flush_all()`, persistence lock after savepoint, state table, concurrency retries (max 5, `_trigger(at=+60s)`), chatter summary.
- [ ] Verify: py_compile + lxml parse.
- [ ] Commit `feat(xe_fifo_payment_reconcile): add FIFO batch reconcile engine and cron`.

### Task 4: README and Spanish translation

**Files:**
- Create: `xe_fifo_payment_reconcile/README.md`, `i18n/es.po`

- [ ] README: purpose, setup (group, partner flag, parameter), states, PUE-only rule, pre-install notes for prototype DBs, manual test plan.
- [ ] `es.po`: model terms (fields, helps, selections, group, cron), view terms (search filters), code terms (`#. odoo-python`, `#, python-format` where applicable).
- [ ] Verify: scratch script parses `es.po` and checks every `_()` msgid in code has a translation with the same placeholders.
- [ ] Commit `feat(xe_fifo_payment_reconcile): add README and es translation`.

### Task 5: Review and hand-off

- [ ] Self-review against spec; re-run all static checks.
- [ ] Ask the user before pushing the branch and opening the PR to `main` (body: summary, pre-install notes, manual test plan incl. "30% Ahora, Balance 60 Días" and in-invoice partial cases).
