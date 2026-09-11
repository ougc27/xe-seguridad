import base64
from datetime import date, datetime
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

import pytz

from ..models.sale_order import MELI_FISCAL_TIMEZONE


@tagged('post_install', '-at_install')
class TestMeliFullCancellationAutomation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Force English: several assertions below check translatable
        # message_post bodies. This database's admin user has a Spanish
        # `lang`, which would otherwise render those via i18n/es.po.
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        # Dedicated test company/data throughout — never touch env.company.
        # Name changed from 'Test Co (meli full cancellation)' (2026-08-31,
        # Fix Round 1): an earlier bug in this task's Phase-1/Phase-2 commit
        # guard caused one real, uncontrolled `cr.commit()` against the
        # shared xeseguridad database before the guard was fixed, which
        # permanently persisted a company with the original name (real
        # leaked row — flagged to the user for manual cleanup, see the
        # Fix Round 1 section of task-3-report.md). res.company.name has a
        # global unique constraint, so re-using the same name here would
        # collide with that leftover row and fail setUpClass.
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli full cancellation) B'})
        # Task 3 needs to post a real invoice on this company (to simulate
        # an already-stamped CFDI) — that requires a sale journal, which
        # only exists once a chart of accounts is loaded. 'generic_coa' is
        # the base Odoo chart (ships in the 'account' module itself, no
        # extra dependency) and sets account_fiscal_country_id to the US —
        # keeping this company non-Mexican on purpose, so
        # l10n_mx_edi_is_cfdi_needed stays False and no real CFDI
        # stamping/signing is ever triggered by posting here.
        cls.env['account.chart.template'].try_loading(
            'generic_coa', company=cls.test_company, install_demo=False,
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Producto Full Cancel Test', 'type': 'product',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_fulfillment = cls.env['stock.warehouse'].create({
            'name': 'Almacen Full Cancel Test', 'code': 'FCXT',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacen Default Cancel Test', 'code': 'DCXT',
            'company_id': cls.test_company.id,
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Cancel Test', 'x_cop': 'cliente',
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id-cancel',
            'client_secret': 'test-client-secret-cancel',
            'state': 'connected',
            'partner_id': cls.partner.id,
            'warehouse_fulfillment_id': cls.warehouse_fulfillment.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _create_full_order(self, order_ref, quantity=1,
                           sync_source='xe_meli_connector'):
        """A confirmed, fully-delivered Full order — the starting state
        every test in this file needs before simulating a cancellation.

        `sync_source` can be set to False to build a legacy (Ventiapp)
        order, which must never be touched by the automation.
        """
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': order_ref,
            'meli_sync_source': sync_source,
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': quantity,
            })],
        })
        order.action_confirm()
        order.picking_ids.button_validate()
        return order

    def test_return_full_pickings_creates_and_validates_a_return(self):
        order = self._create_full_order('FCXT-0001')
        outbound_picking = order.picking_ids

        returns = order._meli_return_full_pickings()

        self.assertEqual(len(returns), 1)
        self.assertEqual(returns.state, 'done')
        self.assertNotEqual(returns, outbound_picking)
        self.assertEqual(
            sum(returns.move_ids.mapped('quantity')),
            sum(outbound_picking.move_ids.mapped('quantity')),
        )

    def test_return_full_pickings_returns_empty_when_nothing_to_return(self):
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FCXT-0002',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()  # picking exists but is not 'done' yet

        returns = order._meli_return_full_pickings()

        self.assertFalse(returns)

    def test_return_full_pickings_idempotent_second_call_does_not_re_return(self):
        """Important #4: Mercado Libre can deliver the same 'cancelled'
        notification more than once (and the polling cron can race a
        webhook), so a second call on an already-returned order must
        return NOTHING AT ALL — not merely avoid returning the return.
        The earlier version of this test tolerated a second return of the
        original outbound picking, which is exactly the double-return
        this now forbids.
        """
        order = self._create_full_order('FCXT-0003')
        original_outbound = order.picking_ids

        # First call: creates a return from the outbound picking
        first_returns = order._meli_return_full_pickings()
        self.assertEqual(len(first_returns), 1)
        # Verify the first return has code != 'outgoing' (it's a return type)
        self.assertEqual(first_returns.state, 'done')
        self.assertNotEqual(first_returns.picking_type_id.code, 'outgoing')
        self.assertTrue(all(
            move.returned_move_ids for move in original_outbound.move_ids
        ))

        second_returns = order._meli_return_full_pickings()

        self.assertFalse(
            second_returns,
            "a second call re-returned stock that had already been "
            "returned — the outbound moves already have returned_move_ids",
        )
        # And nothing new was created behind the scenes either.
        self.assertEqual(len(order.picking_ids), 2)

    def test_return_full_pickings_ignores_a_cancelled_previous_return(self):
        """A return that was cancelled means nothing actually came back,
        so the outbound move is still returnable — the skip is about real
        returns, not about any row that happens to exist.
        """
        order = self._create_full_order('FCXT-0022')
        first_returns = order._meli_return_full_pickings()
        self.assertEqual(len(first_returns), 1)
        # Force the return moves into 'cancel' the way a cancelled
        # transfer would leave them.
        first_returns.move_ids.write({'state': 'cancel'})

        second_returns = order._meli_return_full_pickings()

        self.assertEqual(len(second_returns), 1)
        self.assertNotEqual(second_returns, first_returns)

    def _order_data(self, status='cancelled'):
        return {'status': status}

    def test_full_order_cancelled_without_invoice_is_returned_and_cancelled(self):
        order = self._create_full_order('FCXT-0003')
        picking_count_before = len(order.picking_ids)

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        self.assertEqual(len(order.picking_ids), picking_count_before + 1)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))
        self.assertTrue(any(
            'cancelled' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_full_cancellation_sets_auto_cancellation_processed_flag(self):
        order = self._create_full_order('FCXT-0023')
        self.assertFalse(order.meli_auto_cancellation_processed)

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.meli_auto_cancellation_processed)

    def test_non_full_cancellation_never_sets_auto_cancellation_processed_flag(self):
        order = self._create_non_full_order('FCXT-0024')

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.meli_auto_cancellation_processed)

    def test_failed_full_cancellation_does_not_set_auto_cancellation_processed_flag(self):
        order = self._create_full_order('FCXT-0025')

        with patch.object(
            type(order), '_meli_return_full_pickings',
            side_effect=UserError('boom'),
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertFalse(order.meli_auto_cancellation_processed)

    def test_non_full_order_cancelled_only_gets_the_manual_review_message(self):
        order = self._create_non_full_order('FCXT-0004')

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertTrue(any(
            'Review manually' in (msg.body or '') for msg in order.message_ids
        ))

    def test_partially_refunded_full_order_is_not_automated(self):
        # Scope correction vs. the literal spec text: 'partially_refunded'
        # never carries the returned quantity, so it must NEVER trigger
        # this automation — it stays manual-review-only until Fase C.
        order = self._create_full_order('FCXT-0005')

        order._meli_flag_status_change(self._order_data(status='partially_refunded'))

        self.assertEqual(order.state, 'sale')
        self.assertTrue(any(
            'Review manually' in (msg.body or '') for msg in order.message_ids
        ))

    def test_full_order_cancellation_failure_rolls_back_and_falls_back_to_manual_review(self):
        order = self._create_full_order('FCXT-0006')

        with patch.object(
            type(order), '_meli_return_full_pickings',
            side_effect=UserError('boom'),
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))
        self.assertTrue(any(
            'Review manually' in (msg.body or '') for msg in order.message_ids
        ))

    def _create_non_full_order(self, order_ref):
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': order_ref,
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        return order

    def _chatter(self, order):
        return '\n'.join(msg.body or '' for msg in order.message_ids).lower()

    def test_no_posted_invoice_is_only_returned_and_cancelled(self):
        order = self._create_full_order('FCXT-0008')
        order._create_invoices()  # left in draft, never posted

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')

    def test_legacy_order_without_sync_source_is_never_automated(self):
        """Important #3a: a legacy Ventiapp order sitting in the same Full
        warehouse must only get the manual-review message. Before the
        automation existed this code path was harmless; now it returns
        stock and cancels sales.
        """
        order = self._create_full_order('FCXT-0015', sync_source=False)
        picking_count_before = len(order.picking_ids)

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertEqual(len(order.picking_ids), picking_count_before)
        self.assertIn('review manually', self._chatter(order))

    def test_disconnected_configuration_does_not_authorize_the_automation(self):
        """Important #3b: the config lookup must require
        state == 'connected', like every other config lookup in this
        module.
        """
        order = self._create_full_order('FCXT-0016')
        self.config.state = 'error'
        picking_count_before = len(order.picking_ids)

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'sale')
        self.assertEqual(len(order.picking_ids), picking_count_before)
        self.assertIn('review manually', self._chatter(order))

    def test_recover_aborted_transaction_rolls_back_only_when_allowed(self):
        """Important #1, the guard itself: the rollback must be gated by
        the same _can_commit() check as the Phase 1 commit, or it would
        destroy the fixtures of every test that ever reaches it.
        """
        order = self._create_full_order('FCXT-0017')
        document_model = type(self.env['l10n_mx_edi.document'])

        with patch.object(type(self.env.cr), 'rollback') as mock_rollback:
            order._meli_recover_aborted_transaction()
        mock_rollback.assert_not_called()

        with patch.object(
            document_model, '_can_commit', return_value=True,
        ), patch.object(type(self.env.cr), 'rollback') as mock_rollback:
            order._meli_recover_aborted_transaction()
        mock_rollback.assert_called_once()

    def test_real_database_error_still_posts_a_chatter_message(self):
        """Important #1: a genuine PostgreSQL error inside Phase 2 (not
        just a Python exception) aborts the transaction, and every later
        query — including the chatter message the operator needs — then
        fails with InFailedSqlTransaction. The operator would be left
        with nothing but a failed queue job.

        Under tests the production rollback is a deliberate no-op (a real
        one would throw away this test's own fixtures), so the recovery
        hook is patched with the test-scoped equivalent: ROLLBACK TO a
        savepoint taken just before the call. What that verifies is
        exactly the bug — the hook is reached on this path, before
        message_post — plus the fact that once the transaction is
        recovered, the message really does get posted. The guard's own
        _can_commit() gating is covered by the test above.
        """
        order = self._create_full_order('FCXT-0018')
        self.env.flush_all()
        self.env.cr.execute('SAVEPOINT meli_i1_test')
        recovered = []

        def _recover(*args, **kwargs):
            self.env.cr.execute('ROLLBACK TO SAVEPOINT meli_i1_test')
            # What the real cr.rollback() also does: drop the ORM caches
            # and any pending writes, which no longer match the database.
            self.env.cr.clear()
            recovered.append(True)

        def _abort_the_transaction(*args, **kwargs):
            # A real database error, rejected by PostgreSQL itself rather
            # than by Python: account_move has plenty of NOT NULL columns.
            self.env.cr.execute(
                "INSERT INTO account_move (name) VALUES ('meli-i1-abort')",
                log_exceptions=False,
            )

        with patch.object(
            type(order), '_meli_reconcile_invoicing',
            side_effect=_abort_the_transaction,
        ), patch.object(
            type(order), '_meli_recover_aborted_transaction',
            side_effect=_recover,
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertTrue(
            recovered,
            "the transaction-recovery hook was never reached — the "
            "chatter message would have died with InFailedSqlTransaction",
        )
        self.assertIn('manual review', self._chatter(order))

    def test_invoice_reconciliation_failure_does_not_roll_back_stock_and_sale(self):
        # The load-bearing case for the two-phase design: Odoo's own
        # l10n_mx_edi bookkeeping methods reused by
        # _meli_reconcile_invoicing (e.g.
        # _l10n_mx_edi_cfdi_invoice_document_cancel /
        # _l10n_mx_edi_cfdi_invoice_document_sent) can force their own
        # commit in production, which would silently invalidate a
        # savepoint shared with invoice reconciliation. Phase 1 (stock
        # return + sale cancellation) is committed BEFORE Phase 2
        # (invoice reconciliation) is attempted, so a Phase 2 failure must
        # never undo Phase 1's already-committed work.
        order = self._create_full_order('FCXT-0010')
        picking_count_before = len(order.picking_ids)

        with patch.object(
            type(order), '_meli_reconcile_invoicing',
            side_effect=Exception('boom'),
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        self.assertEqual(len(order.picking_ids), picking_count_before + 1)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))
        messages = '<br/>'.join(msg.body or '' for msg in order.message_ids)
        self.assertIn('cancelled', messages.lower())
        self.assertIn('manual review', messages.lower())

    def test_cancelled_on_arrival_full_without_documents_creates_nothing(self):
        order_data = {
            'id': 'FCXT-0026', 'status': 'cancelled', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-CXT26', 'seller_sku': 'ZTEST-NOT-MAPPED-CXT26'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ):
            result = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertFalse(result)
        self.assertFalse(self.env['sale.order'].search([
            ('meli_order_id', '=', 'FCXT-0026'),
        ]))

    def test_cancelled_on_arrival_non_full_creates_and_confirms_only(self):
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-CXT27',
        })
        order_data = {
            'id': 'FCXT-0027', 'status': 'cancelled', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-CXT27', 'seller_sku': 'ZTEST-CXT27'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertTrue(order)
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.warehouse_id, self.warehouse_default)
        # Odoo's own action_confirm() always creates the delivery picking
        # via the normal stock rule, whether or not this task calls
        # _meli_ensure_delivery — that's expected and correct (the spec's
        # "don't force it" means don't auto-validate/short-circuit
        # reservation, not "prevent the ordinary picking from existing").
        # What this task must NOT do is auto-validate it, or run any
        # cancellation/return/credit-note automation.
        self.assertTrue(order.picking_ids)
        self.assertTrue(all(p.state != 'done' for p in order.picking_ids))
        self.assertFalse(order.meli_auto_cancellation_processed)


@tagged('post_install', '-at_install')
class TestMeliInvoicingLifecycle(TransactionCase):
    """Covers sale.order._meli_reconcile_invoicing /
    _meli_relate_invoice_document (Task 2 of the Full invoicing lifecycle
    plan) — invoice creation and refacturación detection only, called
    directly (not yet wired to real triggers, that's Task 3).

    Unlike TestMeliFullCancellationAutomation above (which deliberately
    uses generic_coa/US to AVOID triggering real CFDI logic), this class
    uses a genuinely Mexican chart of accounts so
    l10n_mx_edi_is_cfdi_needed actually engages, and every test below
    patches account.move._l10n_mx_edi_cfdi_invoice_try_send (Odoo's own
    real PAC-calling method) to raise if it's ever called — the direct
    proof that this task's code relates Mercado Libre's own XML instead
    of ever calling Odoo's own PAC. See the setUpClass comment by
    `cls.partner` below for why res.partner.cfdi_issued_by_third_party
    itself (xe_l10n_mx_edi) could not also be exercised here.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoicing lifecycle)'})
        # Mexican chart of accounts — NOT 'mx_coa' (that's not a valid
        # template code in this Odoo version's chart-template registry).
        # Confirmed by reading odoo/addons/l10n_mx/models/template_mx.py:
        # the @template(...) decorator registers this chart under the
        # code 'mx', and enterprise/l10n_mx_edi/tests/common.py's own
        # AccountTestInvoicingCommon.setUpClass(chart_template_ref='mx')
        # confirms the same code is what l10n_mx_edi's own test suite
        # uses to get a real Mexican fiscal setup.
        cls.env['account.chart.template'].try_loading(
            'mx', company=cls.test_company, install_demo=False,
        )
        cls.product = cls.env['product.product'].create({
            'name': 'Producto Invoicing Test', 'type': 'product',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_fulfillment = cls.env['stock.warehouse'].create({
            'name': 'Almacen Full Invoicing Test', 'code': 'FIVT',
            'company_id': cls.test_company.id,
        })
        # Second, non-fulfillment warehouse — needed so a sale.order can
        # be built that is genuinely NOT a Full order (warehouse_id !=
        # config.warehouse_fulfillment_id), same pattern as
        # TestMeliFullCancellationAutomation's own warehouse_default
        # above in this same file.
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacen Default Invoicing Test', 'code': 'DIVT',
            'company_id': cls.test_company.id,
        })
        # Deliberately NOT setting 'x_cop' here, unlike the other test
        # classes in this file/module (test_meli_full_cancellation.py's
        # own TestMeliFullCancellationAutomation, test_cron.py,
        # test_sale_order_meli.py, test_meli_import_batch.py): 'x_cop' is
        # a field from xe_pacific (a base_automation rule elsewhere reads
        # it to gate sale.order confirmation), and xe_pacific is not
        # installed in this shared test database — confirmed in practice
        # (2026-09-08): setting it raises "Invalid field 'x_cop' on model
        # 'res.partner'" and is exactly why those OTHER classes are
        # already part of this suite's known, pre-existing baseline
        # errors (see the Global Constraints: "6 failed, 5 error(s)").
        # Since xe_pacific is uninstalled here, the base_automation rule
        # itself doesn't exist in this database either (it's xe_pacific's
        # own data), so there is nothing actually gating action_confirm()
        # below — omitting 'x_cop' is safe, not a workaround for a real
        # blocker.
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Invoicing Test',
        })
        # Needed for Task 6's pack-consolidation tests below, which build
        # their fixture through the real _meli_create_from_order_data
        # entry point (unlike every other test in this class, which
        # builds orders directly) — that method reads
        # config.sale_team_id/salesperson_id (both `... or False`, so
        # omitting them wouldn't error, but a real Mercado Libre
        # connection always sets them, and TestSaleOrderMeliImport's own
        # setUpClass — the one other place in this suite that exercises
        # this same entry point — sets them too; matched here for
        # consistency).
        cls.salesperson = cls.env['res.users'].create({
            'name': 'Meli Invoicing Test User', 'login': 'meli_invoicing_test_user',
        })
        cls.team = cls.env['crm.team'].create({
            'name': 'MARKETPLACE Invoicing Test', 'company_id': cls.test_company.id,
        })
        # NOT setting res.partner.cfdi_issued_by_third_party here — it
        # would be the other half of what makes this a genuinely Mexican
        # fiscal test (see the class docstring), but the field lives on
        # xe_l10n_mx_edi, and xe_l10n_mx_edi cannot be installed in this
        # shared test database under the addons-path this suite is
        # required to run with: it depends on xe_pacific, whose own
        # __manifest__.py depends list is missing 'crm' even though
        # xe_pacific/models/helpdesk_ticket.py references crm.lead —
        # confirmed in practice (2026-09-08): `-i xe_l10n_mx_edi` fails
        # registry load with "Model 'crm.lead' does not exist in
        # registry." This is a pre-existing gap in xe_pacific's own
        # manifest, unrelated to and out of scope for this task — not
        # something to patch here. What this means for THIS test class:
        # it cannot exercise xe_l10n_mx_edi's block as a second,
        # independent witness that the PAC was never reached. It doesn't
        # weaken the actual proof, though — every test below still
        # patches account.move._l10n_mx_edi_cfdi_invoice_try_send (the
        # real PAC-calling method) to raise if it's ever called, which is
        # the direct, load-bearing assertion that Mercado Libre's own XML
        # is what ends up related to the invoice, never Odoo's own
        # stamping. The 'mx' chart of accounts is still real and load-
        # bearing on its own: it's what makes l10n_mx_edi_is_cfdi_needed
        # true and l10n_mx_edi_cfdi_uuid/l10n_mx_edi_cfdi_attachment_id
        # genuinely compute from the related XML, instead of trivially
        # passing on a non-Mexican company that never looks at CFDI
        # fields at all.
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id-invoicing',
            'client_secret': 'test-client-secret-invoicing',
            'state': 'connected',
            'partner_id': cls.partner.id,
            'sale_team_id': cls.team.id,
            'salesperson_id': cls.salesperson.id,
            'warehouse_fulfillment_id': cls.warehouse_fulfillment.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _create_delivered_order(self, order_ref, meli_order_id=None):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': order_ref,
            'meli_order_id': meli_order_id or order_ref,
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        order.picking_ids.button_validate()
        return order

    def _fake_cfdi_xml(self, folio, fecha='2026-09-08T10:00:00'):
        """A minimal but structurally real CFDI 4.0 — NOT just a root
        node with no content: account.move._compute_l10n_mx_edi_cfdi_uuid
        reads l10n_mx_edi_cfdi_uuid from the cfdi:Complemento/
        tfd:TimbreFiscalDigital node's UUID attribute (via
        l10n_mx_edi.document._decode_cfdi_attachment, which locates it by
        local-name() regardless of namespace prefix) — a Comprobante with
        no TimbreFiscalDigital at all decodes to an empty uuid, which
        would make this fixture useless for proving a real UUID got
        related. The UUID is derived from folio so different documents
        in the same test get different, distinguishable UUIDs.

        `fecha` (the CFDI's own Fecha attribute, Monterrey/CDMX local
        time by SAT rule — see meli.invoice.document.
        _meli_parse_issue_date_from_xml) is overridable so Fix 4's own
        tests can pick a value that deliberately differs from "today"
        and from any other date already in play.
        """
        uuid = f'AAAAAAAA-0000-0000-0000-{int(folio):012d}'
        return (
            '<cfdi:Comprobante '
            'xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
            'xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital" '
            f'Version="4.0" Folio="{folio}" Fecha="{fecha}" '
            'SubTotal="100.00" Total="116.00" Sello="fake-sello">'
            '<cfdi:Emisor Rfc="XEB010101AA1" Nombre="XE Brands" RegimenFiscal="601"/>'
            '<cfdi:Receptor Rfc="XAXX010101000" Nombre="Publico en general" '
            'UsoCFDI="G03"/>'
            '<cfdi:Complemento>'
            f'<tfd:TimbreFiscalDigital Version="1.1" UUID="{uuid}" '
            'FechaTimbrado="2026-09-08T10:00:01" SelloCFD="fake-sello-cfd" '
            'NoCertificadoSAT="00000000000000000000" SelloSAT="fake-sello-sat"/>'
            '</cfdi:Complemento>'
            '</cfdi:Comprobante>'
        ).encode()

    def test_meli_import_order_relinks_a_document_that_arrived_before_the_order(self):
        """The bug this closes (found 2026-09-10, from a real production
        document stuck with sale_order_id blank): meli.invoice.document.
        sale_order_id is a stored compute field with
        @api.depends('meli_order_id') only — its own field, never
        anything about sale.order — so it never recomputes on its own
        just because a matching sale.order gets created later. Before
        this fix, only sale.order._meli_recover_cancelled_on_arrival_full
        (2026-09-09) ever forced this recompute, and only for its own
        narrow "arrived already cancelled" case — a document that
        arrived before a completely NORMAL (paid, never cancelled) order
        got created had nothing forcing its own recompute at all.
        """
        document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-RELINK1', 'transaction_type': 'sale',
            'meli_invoice_id': '9400000000000001',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('950')),
        })
        self.assertFalse(document.sale_order_id, "no order exists yet")
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-RELINK1',
        })
        order_data = {
            'id': 'FIVT-RELINK1', 'status': 'paid', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-RELINK1', 'seller_sku': 'ZTEST-RELINK1'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }

        with patch.object(
            type(self.config), '_api_get', return_value=order_data,
        ):
            order = self.env['sale.order'].sudo()._meli_import_order(
                self.test_company.id, 'FIVT-RELINK1',
            )

        self.assertTrue(order)
        self.assertEqual(document.sale_order_id, order)

    def test_reconcile_creates_and_relates_invoice_never_stamps(self):
        order = self._create_delivered_order('FIVT-0001')
        document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0001', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000001',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('677')),
        })
        self.assertEqual(document.sale_order_id, order)

        with patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_send',
            side_effect=AssertionError("must never be called — Mercado Libre owns the CFDI"),
        ):
            order._meli_reconcile_invoicing()

        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.meli_invoice_document_id, document)
        self.assertTrue(invoice.l10n_mx_edi_cfdi_attachment_id)
        self.assertTrue(invoice.l10n_mx_edi_cfdi_uuid)

    def _cancelled_order_data(self, order_id, pack_id=False):
        # date_created/date_closed left None, matching every other
        # order_data fixture already used throughout this file (e.g.
        # _create_meli_pack's own order_data_a/b) — parsing real
        # timestamps isn't what these tests are about.
        return {
            'id': order_id, 'status': 'cancelled', 'pack_id': pack_id,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-RECOVERY', 'seller_sku': 'ZTEST-RECOVERY'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }

    def test_cancelled_on_arrival_full_with_invoice_runs_full_pipeline(self):
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-RECOVERY',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-RECOVERY-0001', 'transaction_type': 'sale',
            'meli_invoice_id': '9100000000000001',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('800')),
        })
        self.assertFalse(document.sale_order_id, "no sale order should exist yet")

        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_send',
            side_effect=AssertionError("must never be called — Mercado Libre owns the CFDI"),
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, self._cancelled_order_data('FIVT-RECOVERY-0001'),
            )

        self.assertTrue(order)
        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.meli_auto_cancellation_processed)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))
        self.assertEqual(len(order.picking_ids), 2, "outbound + return")
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.meli_invoice_document_id, document)

    def test_cancelled_on_arrival_full_with_invoice_and_credit_note_relates_both(self):
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-RECOVERY2',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        invoice_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-RECOVERY-0002', 'transaction_type': 'sale',
            'meli_invoice_id': '9100000000000002',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('801')),
        })
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-RECOVERY-0002', 'transaction_type': 'devolution',
            'meli_invoice_id': '9100000000000003',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('802')),
        })

        order_data = self._cancelled_order_data('FIVT-RECOVERY-0002')
        order_data['order_items'][0]['item']['seller_sku'] = 'ZTEST-RECOVERY2'
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_send',
            side_effect=AssertionError("must never be called — Mercado Libre owns the CFDI"),
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.meli_auto_cancellation_processed)
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.meli_invoice_document_id, invoice_document)
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.meli_invoice_document_id, credit_note_document)

    def test_cancelled_on_arrival_full_pack_order_skips_processing_for_manual_review(self):
        """Final whole-branch review (2026-09-09), Critical C2:
        _meli_recover_cancelled_on_arrival_full must never run the
        whole-order destructive pipeline on a pack order recovered this
        way — there is no "other sibling already known and still paid"
        state to protect the way _meli_flag_status_change's own
        is_full-and-meli_pack_id branch protects a mid-life partial
        cancellation, so running it anyway would let a LATER, genuinely
        'paid' sibling get silently grafted onto this already-cancelled
        order by _meli_add_pack_sibling_lines (which runs regardless of
        state) and lose its own delivery/invoice/revenue with no trace.

        Same fixture setup as this test used to have before the fix
        (formerly named
        ..._document_filed_under_pack_id_still_resolves): a document
        already filed under the pack id, then the order created via
        _meli_create_from_order_data with that same pack_id and status
        'cancelled'. Before this fix, this exact scenario resulted in
        order.state == 'cancel' and meli_auto_cancellation_processed ==
        True — now it must stay 'sale', unprocessed, with a manual-review
        chatter message instead.

        Deliberately does not assert on order.invoice_ids either way:
        meli.invoice.document.sale_order_id is a stored compute field
        that, within this single test transaction, ends up resolving to
        this order regardless of whether this fix's own early return
        skips _meli_recover_cancelled_on_arrival_full's explicit
        recompute step — client_order_ref/reference are set to the pack
        id for every pack order (see _meli_create_from_order_data), so a
        later, ordinary ORM flush (e.g. stock_picking.py's own
        _action_done() hook, triggered by this same call's delivery
        validation, which runs BEFORE this method is ever reached)
        resolves it via that same tier lazily. That is a same-transaction
        test-timing artifact, not something this fix controls: in
        production the document row is created and committed by an
        entirely separate request/transaction, so it is never dirty by
        the time this one runs and stays unresolved until something
        explicitly recomputes it. What this fix actually controls —
        and what production cares about — is proven by the three
        assertions below: the whole-order destructive pipeline
        (_meli_process_full_cancellation) must never run.
        """
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-RECOVERY3',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-RECOVERY-PACK3', 'transaction_type': 'sale',
            'meli_invoice_id': '9100000000000004',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('803')),
        })

        order_data = self._cancelled_order_data(
            'FIVT-RECOVERY-0003', pack_id='FIVT-RECOVERY-PACK3',
        )
        order_data['order_items'][0]['item']['seller_sku'] = 'ZTEST-RECOVERY3'
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_send',
            side_effect=AssertionError("must never be called — Mercado Libre owns the CFDI"),
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertTrue(order)
        self.assertEqual(order.meli_pack_id, 'FIVT-RECOVERY-PACK3')
        self.assertEqual(order.state, 'sale', "must NOT be run through the whole-order pipeline")
        self.assertFalse(order.meli_auto_cancellation_processed)
        pack_messages = order.message_ids.filtered(
            lambda m: 'pack' in (m.body or '') and 'cancelled' in (m.body or '')
        )
        self.assertTrue(pack_messages, "a manual-review chatter message mentioning the pack must be posted")

    def test_reconcile_does_nothing_without_a_document_yet(self):
        order = self._create_delivered_order('FIVT-0002')

        order._meli_reconcile_invoicing()

        self.assertFalse(order.invoice_ids)

    def test_reconcile_does_nothing_when_newest_document_lacks_xml(self):
        """Reviewer finding (2026-09-08): meli_invoice_document.py itself
        deliberately upserts a meli.invoice.document with xml_file=False
        when Mercado Libre's own XML fetch 404s ("storing the record
        without a file for manual follow-up" —
        _meli_import_invoice_document, ~line 379-393) — an existing
        caller in that same file already guards against exactly this
        (_meli_import_invoice_document_for_batch_line: "if not document
        or not document.xml_file"). Before this fix, the reconciler's
        document search had no equivalent filter: it would find this
        XML-less document, create and post a brand new invoice, and
        only THEN crash on base64.b64decode(False) inside
        _meli_relate_invoice_document — leaving a half-done invoice with
        no CFDI relation. The fix treats a document without XML exactly
        like no document at all (it isn't "ready" yet): this test
        proves no invoice gets created and nothing crashes.
        """
        order = self._create_delivered_order('FIVT-0004')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0004', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000020',
            'xml_file': False,
        })

        order._meli_reconcile_invoicing()

        self.assertFalse(order.invoice_ids)

    def test_reconcile_refactura_cancels_old_invoice_and_creates_new(self):
        """Confirmed fix (2026-09-08, controller-provided, verified against
        real Odoo core before use): a bare button_cancel() on
        current_invoice fails once it already carries a related Mercado
        Libre CFDI (l10n_mx_edi_cfdi_state='sent' makes Odoo core's own
        account.move._l10n_mx_edi_need_cancel_request() true — confirmed
        in practice while first writing this test).
        button_request_cancel() isn't the answer either: for MX it only
        opens a cancellation WIZARD (l10n_mx_edi.document.action_cancel
        -> action_request_cancel returns a UI action dict, doesn't touch
        state) and finishing that flow for real would ask Odoo's own
        l10n_mx_edi session to request an actual SAT cancellation for a
        CFDI Odoo never sent — exactly what "Mercado Libre is the only
        source of the CFDI" forbids. The real fix reuses
        account.move._l10n_mx_edi_cfdi_invoice_document_cancel(cfdi,
        cancel_reason) — the same method l10n_mx_edi itself uses to
        record a cancellation OUTCOME after a genuine PAC cancel call —
        purely as local bookkeeping: it creates a new
        l10n_mx_edi.document (state='invoice_cancel') reusing the SAME
        attachment the invoice already has, no new attachment, no PAC
        call. That's what makes l10n_mx_edi_cfdi_state stop reading
        'sent', which is what makes the ordinary button_cancel() that
        follows succeed. Both halves are asserted below: the real
        PAC-cancel method is mock-patched to raise if ever called
        (mirroring the never-stamps assertion in the first test in this
        class), proving Mercado Libre's already-issued CFDI is never
        touched at the SAT — only Odoo's own local invoice record is
        superseded.
        """
        order = self._create_delivered_order('FIVT-0003')
        first_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0003', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000010',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('700')),
        })
        order._meli_reconcile_invoicing()
        first_invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        first_document_id = first_invoice.l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_sent'
        )
        self.assertTrue(first_document_id, "first invoice should carry a live CFDI document")

        second_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0003', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000011',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('701')),
        })
        with patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_cancel',
            side_effect=AssertionError(
                "must never be called — cancelling a refactured invoice "
                "is local bookkeeping only, never a real SAT/PAC "
                "cancellation of Mercado Libre's own CFDI"
            ),
        ):
            order._meli_reconcile_invoicing()

        self.assertEqual(first_invoice.state, 'cancel')
        # _l10n_mx_edi_cfdi_invoice_document_cancel creates a NEW
        # l10n_mx_edi.document (state='invoice_cancel') rather than
        # mutating the original 'invoice_sent' one in place — same
        # pattern as a genuine PAC cancel flow (see
        # _meli_reconcile_invoicing's own docstring in sale_order.py,
        # the refacturación comment: "the original ... document stays in
        # 'invoice_sent' either way"). The newest document for this
        # invoice is the one that actually reflects the cancel, and it
        # must reuse the SAME attachment as the original — never
        # re-attached, never re-sent to any PAC.
        first_invoice.invalidate_recordset(['l10n_mx_edi_invoice_document_ids'])
        newest_document = first_invoice.l10n_mx_edi_invoice_document_ids.sorted()[:1]
        self.assertEqual(newest_document.state, 'invoice_cancel')
        self.assertEqual(newest_document.attachment_id, first_document_id.attachment_id)
        self.assertEqual(first_document_id.state, 'invoice_sent')
        new_invoices = order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )
        self.assertEqual(len(new_invoices), 1)
        self.assertEqual(new_invoices.meli_invoice_document_id, second_document)
        self.assertNotEqual(new_invoices, first_invoice)
        self.assertTrue(new_invoices.l10n_mx_edi_cfdi_uuid)

    def test_button_validate_triggers_the_reconciler(self):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-0010',
            'meli_order_id': 'FIVT-0010',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0010', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000020',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('900')),
        })
        self.assertFalse(order.invoice_ids)

        order.picking_ids.button_validate()

        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, 'posted')

    def test_reconcile_relates_credit_note_for_full_order(self):
        order = self._create_delivered_order('FIVT-0020')
        invoice_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0020', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000030',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('800')),
        })
        order._meli_reconcile_invoicing()
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0020', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000031',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('801')),
        })

        order._meli_reconcile_invoicing()

        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.meli_invoice_document_id, credit_note_document)
        self.assertTrue(credit_note.l10n_mx_edi_cfdi_attachment_id)

    def test_reconcile_does_not_relate_credit_note_for_non_full_order(self):
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-0021',
            'meli_order_id': 'FIVT-0021',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        order.picking_ids.button_validate()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0021', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000032',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('810')),
        })
        order._meli_reconcile_invoicing()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0021', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000033',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('811')),
        })

        order._meli_reconcile_invoicing()

        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))
        self.assertTrue(any(
            'devolution' in (msg.body or '').lower() or 'credit note' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_reconcile_credit_note_for_a_zero_line_sibling_does_not_over_refund_the_other_sibling(self):
        """Fix Round 2, Important #2 (narrower recurrence of the Round 1
        Critical bug): the credit-note step's own multi-sibling guard
        must not rely on counting distinct meli_order_id values actually
        present among self.order_line — a sibling can legitimately end
        up with ZERO lines of its own (every one of its SKUs unmapped,
        see _meli_add_pack_sibling_lines's own docstring), and a
        line-derived count silently drops it. Here sibling A (the FIRST
        one imported, entirely unmapped) is exactly that sibling: its
        own order-level meli_order_id still lets a credit note addressed
        to IT resolve straight to this consolidated order (tier 1 of
        meli.invoice.document._compute_sale_order_id — order.meli_order_id
        is set unconditionally at creation, unmapped SKUs or not) even
        though it contributes nothing to a line-derived sibling count.
        Before this fix, that made the guard read this pack as a
        single-sibling order (only sibling B's own line counted) and
        fall through to the whole-invoice account.move.reversal below,
        wrongly over-refunding sibling B's own, completely unrelated
        line.
        """
        # Sibling A: single item, deliberately never mapped -> the order
        # is created (meli_order_id/meli_pack_id set unconditionally,
        # per _meli_create_from_order_data) but gets zero lines. A fully
        # unmapped order is left in 'draft' by that method (the
        # elif order.state == 'draft': action_confirm() branch is
        # skipped whenever unmapped_skus is non-empty) — confirmed here
        # directly since what's under test is the reconciler's own
        # credit-note step, not that unmapped-SKU gating.
        order_data_a = {
            'id': 'FIVT-PACK4-A', 'status': 'paid', 'pack_id': 'FIVT-PACK4',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-UNMAPPED', 'seller_sku': 'ZTEST-UNMAPPED-FIVT4'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.assertFalse(order.order_line, "sibling A must contribute zero lines (unmapped SKU)")
        self.assertEqual(order.meli_order_id, 'FIVT-PACK4-A')
        order.action_confirm()

        # Sibling B: mapped normally, added afterward via
        # _meli_add_pack_sibling_lines — same pattern as
        # _create_meli_pack, built by hand here since sibling A's own
        # setup above needs to deviate from that helper.
        second_product = self.env['product.product'].create({
            'name': 'Producto Invoicing Test (FIVT4-B)', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': 'ZTEST-FIVT4-B',
        })
        self.env['stock.quant']._update_available_quantity(
            second_product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_b = {
            'id': 'FIVT-PACK4-B', 'status': 'paid', 'pack_id': 'FIVT-PACK4',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-B', 'seller_sku': 'ZTEST-FIVT4-B'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK4-B')
        self.assertTrue(line_b)
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()
        # Forced Full only now, AFTER sibling B's own delivery already
        # validated against warehouse_default's locations (same pattern
        # as _create_meli_full_pack elsewhere in this file — reassigning
        # this field doesn't retroactively move an already-created
        # picking). The credit-note step's OWN is_full gate (a separate,
        # pre-existing behavior — credit notes are never automated for
        # non-Full orders at all) would otherwise short-circuit before
        # ever reaching the code path this test targets.
        order.warehouse_id = self.warehouse_fulfillment.id

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK4-B', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000061',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('930')),
        })
        order._meli_reconcile_invoicing()
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(invoice.state, 'posted')

        # The credit note arrives for sibling A — the ZERO-line sibling.
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK4-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000062',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('931')),
        })
        self.assertEqual(
            credit_note_document.sale_order_id, order,
            "sanity check: the document must resolve to the pack order via "
            "tier 1 (sale.order.meli_order_id) despite sibling A having no "
            "lines of its own — otherwise this test wouldn't actually "
            "reach the code path under test",
        )

        order._meli_reconcile_invoicing()

        # The bug this test guards against would have wrongly
        # credit-noted sibling B's own, completely unrelated line via a
        # whole-invoice account.move.reversal.
        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))
        self.assertEqual(line_b.product_uom_qty, 1)
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(any(
            'FIVT-PACK4-A' in (msg.body or '') and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    # ---- Final review fixes (2026-09-08/09) --------------------------

    def test_reconcile_second_credit_note_document_does_not_double_refund(self):
        """Final review Fix 1 (Critical): Mercado Libre can issue a
        REPLACEMENT devolución CFDI for the same cancellation —
        meli.invoice.document._meli_upsert deliberately creates a NEW
        row for this (confirmed against a real ML support case, see its
        own docstring), never overwrites the first. Before this fix, the
        whole-order credit-note step keyed "already handled" purely on
        document identity (meli_invoice_document_id) — the replacement
        being a DIFFERENT row meant this check missed it, and a SECOND,
        real, posted out_refund got created against the same invoice
        lines: an uncapped double refund. The fix checks whether this
        invoice already has a live out_refund at all, regardless of
        which specific document row it's related to, and degrades to a
        manual-review chatter message instead of creating a second one.
        """
        order = self._create_delivered_order('FIVT-0040')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0040', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000080',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('950')),
        })
        order._meli_reconcile_invoicing()
        first_credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0040', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000081',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('951')),
        })
        order._meli_reconcile_invoicing()
        first_refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(first_refunds), 1)
        self.assertEqual(first_refunds.meli_invoice_document_id, first_credit_note_document)

        # Mercado Libre issues a REPLACEMENT devolución for the exact
        # same cancellation — a genuinely different document row (its
        # own meli_invoice_id), the same shape _meli_upsert's own
        # docstring documents for facturas.
        second_credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0040', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000082',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('952')),
        })

        order._meli_reconcile_invoicing()

        refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(refunds), 1, "must never create a SECOND, duplicate credit note")
        self.assertEqual(refunds, first_refunds)
        self.assertTrue(any(
            second_credit_note_document.meli_invoice_id in (msg.body or '')
            and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_reconcile_rejected_credit_note_document_is_never_used(self):
        """Final review Fix 1 (second half): a devolución document whose
        own 'status' field shows the SAT rejected or cancelled it (real,
        webhook-populated values — see meli.invoice.document.status's
        own help text and this model's tree/search views, which already
        treat 'rejected'/'cancelled' as dead) must never be used to
        create a real Odoo credit note.
        """
        order = self._create_delivered_order('FIVT-0041')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0041', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000090',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('960')),
        })
        order._meli_reconcile_invoicing()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0041', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000091',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('961')),
            'status': 'rejected',
        })

        order._meli_reconcile_invoicing()

        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))

    def test_reconcile_credit_note_document_lacking_xml_is_skipped(self):
        """Final review Fix 2: the credit-note step's own document
        search was missing the ('xml_file', '!=', False) guard the
        invoice-relating step's own search already carries (see
        test_reconcile_does_nothing_when_newest_document_lacks_xml
        above) — without it, a credit note could get created and
        posted, then crash on base64.b64decode(False) when trying to
        relate a document that has no XML yet. Same behavior as the
        equivalent invoice-side case: a document without XML isn't
        actable yet, simply skipped.
        """
        order = self._create_delivered_order('FIVT-0042')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0042', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000100',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('970')),
        })
        order._meli_reconcile_invoicing()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0042', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000101',
            'xml_file': False,
        })

        order._meli_reconcile_invoicing()

        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))

    def test_reconcile_older_unrelated_credit_note_document_still_gets_acted_on(self):
        """Final review Fix 3: the credit-note search used to pick only
        the single NEWEST document (limit=1) — an OLDER, never-yet-
        related document (e.g. one that arrived before this order even
        had a posted invoice) got permanently masked once a newer
        document appeared, since the newer one was always evaluated
        instead and the older one was never revisited again. The fix
        iterates every actionable document oldest-first: the older one
        here is the one that actually ends up creating the real credit
        note, never the newer one.
        """
        order = self._create_delivered_order('FIVT-0043')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0043', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000110',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('980')),
        })
        order._meli_reconcile_invoicing()
        older_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0043', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000111',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('981')),
        })
        newer_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0043', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000112',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('982')),
        })

        order._meli_reconcile_invoicing()

        refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(refunds), 1)
        self.assertEqual(
            refunds.meli_invoice_document_id, older_document,
            "the OLDER document must be the one actually used, not masked by the newer one",
        )
        self.assertTrue(any(
            newer_document.meli_invoice_id in (msg.body or '')
            and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def _create_meli_pack(self, pack_id, order_id_a, sku_a, order_id_b, sku_b,
                          second_product=None):
        """Builds a real, consolidated pack sale.order through the actual
        _meli_create_from_order_data entry point (Task 6's tests are the
        first in this class to exercise it) — two individual Mercado
        Libre orders sharing one pack_id end up as ONE sale.order with
        two lines, each stamped with its own meli_order_id, exactly
        matching the real production shape confirmed 2026-09-08 (10 real
        packs with one sibling 'paid' and another 'cancelled', both
        still active). shipping={} makes _meli_fetch_logistic_type
        return None with no API call at all (shipping_id is falsy), so
        _meli_create_from_order_data resolves this as a NON-Full order
        and uses config.warehouse_default_id — irrelevant to
        _meli_process_partial_cancellation itself (it doesn't gate on
        warehouse), and it's what keeps the outbound picking from being
        auto-validated at creation, matching the explicit
        `picking_ids.button_validate()` call every caller below makes.
        Stock is provisioned on THAT same warehouse (warehouse_default,
        not warehouse_fulfillment) for exactly that reason.
        """
        second_product = second_product or self.env['product.product'].create({
            'name': f'Producto Invoicing Test ({sku_b})', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': sku_a,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': sku_b,
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        self.env['stock.quant']._update_available_quantity(
            second_product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_a = {
            'id': order_id_a, 'status': 'paid', 'pack_id': pack_id,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-A', 'seller_sku': sku_a},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order_data_b = {
            'id': order_id_b, 'status': 'paid', 'pack_id': pack_id,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-B', 'seller_sku': sku_b},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        # Sibling A's own move gets reserved automatically as part of
        # action_confirm()'s own flow (run while the order was still
        # 'draft', inside _meli_create_from_order_data). Sibling B's own
        # line is added afterward, via a plain write() on an ALREADY-
        # confirmed order (_meli_add_pack_sibling_lines) — core
        # sale_stock's stock rule still creates a move/picking for it,
        # but nothing re-runs the reservation step automatically the way
        # action_confirm() does, so it's left 'confirmed'
        # (unreserved) until something calls action_assign() on it.
        # Harmless/idempotent for the already-reserved moves too.
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()
        return order, second_product

    def test_partial_cancellation_only_touches_the_cancelled_siblings_line(self):
        order, _second_product = self._create_meli_pack(
            'FIVT-PACK', 'FIVT-PACK-A', 'ZTEST-FIVT', 'FIVT-PACK-B', 'ZTEST-FIVT-2',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK-B')
        # No credit-note document exists yet for this sibling — this
        # covers the "physical return happens before Mercado Libre's own
        # credit-note webhook arrives" ordering too: stock/quantity must
        # still be resolved correctly even with nothing yet to credit.
        outbound_picking = order.picking_ids

        order._meli_process_partial_cancellation('FIVT-PACK-B')

        self.assertEqual(order.state, 'sale')  # the sale itself is NOT cancelled
        self.assertEqual(line_a.product_uom_qty, 1)  # sibling A untouched
        self.assertEqual(line_b.product_uom_qty, 0)  # returned in full
        returns = order.picking_ids.filtered(
            lambda p: p.picking_type_id.code == 'incoming'
        )
        self.assertTrue(returns)
        self.assertEqual(returns.state, 'done')
        # Only sibling B's own move was returned — sibling A's own
        # delivered move must still show no return of its own.
        line_a_move = outbound_picking.move_ids.filtered(lambda m: m.sale_line_id == line_a)
        self.assertFalse(line_a_move.returned_move_ids)
        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))
        self.assertTrue(any(
            'FIVT-PACK-B' in (msg.body or '') for msg in order.message_ids
        ))

    def test_partial_cancellation_is_idempotent(self):
        """Mercado Libre can (and does) redeliver the same status-change
        notification more than once — a second call must not re-return
        already-returned stock or touch the already-adjusted quantity
        again.
        """
        order, _second_product = self._create_meli_pack(
            'FIVT-PACK-IDEM', 'FIVT-PACK-IDEM-A', 'ZTEST-FIVT-IDEM-A',
            'FIVT-PACK-IDEM-B', 'ZTEST-FIVT-IDEM-B',
        )
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK-IDEM-B')
        order._meli_process_partial_cancellation('FIVT-PACK-IDEM-B')
        picking_count = len(order.picking_ids)
        self.assertEqual(line_b.product_uom_qty, 0)

        order._meli_process_partial_cancellation('FIVT-PACK-IDEM-B')

        self.assertEqual(len(order.picking_ids), picking_count)
        self.assertEqual(line_b.product_uom_qty, 0)

    def test_partial_cancellation_second_call_chatter_does_not_claim_never_delivered(self):
        """Fix Round 2, Important #1: new_pickings alone can't tell apart
        "never delivered at all" from "already delivered AND already
        returned in an earlier run" — both leave new_pickings empty on
        THIS call. Round 1's own Finding 2 fix (the same-status dedupe no
        longer swallows a second notification for a pack sibling) makes
        this genuinely reachable in practice: Mercado Libre re-sending
        the same 'cancelled' notification for a sibling that was already
        fully processed must not have its chatter message falsely claim
        the transfer "never completed" / "no delivered stock" when it
        plainly was delivered and already returned, the first time.
        """
        order, _second_product = self._create_meli_pack(
            'FIVT-PACK-CHAT', 'FIVT-PACK-CHAT-A', 'ZTEST-FIVT-CHAT-A',
            'FIVT-PACK-CHAT-B', 'ZTEST-FIVT-CHAT-B',
        )
        order._meli_process_partial_cancellation('FIVT-PACK-CHAT-B')

        order._meli_process_partial_cancellation('FIVT-PACK-CHAT-B')

        second_call_message = order.message_ids.sorted('id', reverse=True)[0]
        body = (second_call_message.body or '').lower()
        self.assertNotIn('never completed', body)
        self.assertNotIn('no delivered stock', body)
        self.assertIn('already returned', body)

    def test_partial_cancellation_credit_note_covers_only_the_cancelled_siblings_line(self):
        """Correction 1 (preflight review, 2026-09-08): the shared
        account.move.reversal-based credit-note mechanism from
        _meli_reconcile_invoicing performs a FULL reversal that mirrors
        every line of the source invoice — wrong for a partial pack
        cancellation, where sibling A's own line/revenue must stay
        completely untouched. This builds a REAL invoice covering BOTH
        siblings' lines (the real, confirmed production shape:
        _create_invoices() invoices the whole order by default, with no
        filtering by meli_order_id) and proves the credit note
        _meli_process_partial_cancellation creates contains ONLY sibling
        B's own line/amount — never sibling A's.
        """
        order, second_product = self._create_meli_pack(
            'FIVT-PACK2', 'FIVT-PACK2-A', 'ZTEST-FIVT2-A', 'FIVT-PACK2-B', 'ZTEST-FIVT2-B',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK2-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK2-B')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK2-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000041',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('910')),
        })
        order._meli_reconcile_invoicing()
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        invoice_product_lines = invoice.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(len(invoice_product_lines), 2, "the invoice must cover BOTH siblings' lines")
        invoice_line_b = invoice_product_lines.filtered(lambda l: l.product_id == second_product)
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK2-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000042',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('911')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK2-B')

        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.state, 'posted')
        self.assertEqual(credit_note.meli_invoice_document_id, credit_note_document)
        self.assertEqual(credit_note.reversed_entry_id, invoice)
        credit_lines = credit_note.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(len(credit_lines), 1, "the credit note must cover ONLY sibling B's own line")
        self.assertEqual(credit_lines.product_id, second_product)
        self.assertEqual(credit_lines.sale_line_ids, line_b)
        self.assertNotIn(self.product, credit_note.invoice_line_ids.mapped('product_id'))
        self.assertAlmostEqual(credit_note.amount_total, invoice_line_b.price_total, places=2)
        self.assertTrue(credit_note.l10n_mx_edi_cfdi_attachment_id)
        # Sibling A's own line, quantity and invoiced amount are
        # completely untouched.
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(line_b.product_uom_qty, 0)
        self.assertEqual(
            invoice.invoice_line_ids.filtered(lambda l: l.product_id == self.product).quantity, 1,
        )

    def test_partial_cancellation_second_credit_note_document_upsert_does_not_double_refund(self):
        """Correction 2's own finding (see task-6-report.md): a return
        picking's move keeps the same procurement group as the original
        delivery move, so stock.picking.sale_id resolves back to this
        same consolidated order — meaning
        xe_meli_connector.stock.picking's own _action_done() override
        re-runs the SHARED, whole-order _meli_reconcile_invoicing() when
        the return picking created below completes. Proves that re-run
        is genuinely harmless here: it must find this sibling's credit
        note already related (by meli_invoice_document_id) and skip,
        never creating a SECOND, duplicate credit note via its own
        (whole-invoice) reversal path.
        """
        order, second_product = self._create_meli_pack(
            'FIVT-PACK3', 'FIVT-PACK3-A', 'ZTEST-FIVT3-A', 'FIVT-PACK3-B', 'ZTEST-FIVT3-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK3-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000051',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('920')),
        })
        order._meli_reconcile_invoicing()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK3-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000052',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('921')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK3-B')

        credit_notes = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(
            len(credit_notes), 1,
            "the return picking's own completion re-ran the shared "
            "reconciler and created a SECOND, duplicate credit note",
        )
        self.assertEqual(
            len(credit_notes.invoice_line_ids.filtered(lambda l: l.display_type == 'product')), 1,
        )

    def _create_meli_full_pack(self, pack_id, order_id_a, sku_a, order_id_b, sku_b):
        """Same as _create_meli_pack, but forces warehouse_id to the
        fulfillment warehouse afterward so _meli_flag_status_change's own
        is_full check reads True — matching every OTHER Full-order test
        in this file (TestMeliFullCancellationAutomation._create_full_
        order), which also sets warehouse_id directly rather than
        exercising the real /shipments logistic-type fetch.
        """
        order, second_product = self._create_meli_pack(pack_id, order_id_a, sku_a, order_id_b, sku_b)
        order.warehouse_id = self.warehouse_fulfillment.id
        return order, second_product

    def test_partial_cancellation_wired_through_flag_status_change(self):
        order, _second_product = self._create_meli_full_pack(
            'FIVT-PACKW', 'FIVT-PACKW-A', 'ZTEST-FIVTW-A', 'FIVT-PACKW-B', 'ZTEST-FIVTW-B',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKW-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKW-B')

        order._meli_flag_status_change({'id': 'FIVT-PACKW-B', 'status': 'cancelled'})

        self.assertEqual(order.state, 'sale')
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(line_b.product_uom_qty, 0)
        returns = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'incoming')
        self.assertTrue(returns)
        self.assertEqual(returns.state, 'done')

    def test_partial_cancellation_failure_falls_back_to_manual_review_never_cancels_the_sale(self):
        """The hazard this wiring must avoid: on a FAILED partial
        cancellation, falling through into the whole-order `is_full`
        cancellation block right below it would cancel the ENTIRE sale
        and return EVERY line's stock — wiping out sibling A even though
        only sibling B was ever reported as cancelled. This proves the
        failure path degrades to manual review only.
        """
        order, _second_product = self._create_meli_full_pack(
            'FIVT-PACKF', 'FIVT-PACKF-A', 'ZTEST-FIVTF-A', 'FIVT-PACKF-B', 'ZTEST-FIVTF-B',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKF-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKF-B')
        picking_count_before = len(order.picking_ids)

        with patch.object(
            type(order), '_meli_process_partial_cancellation',
            side_effect=UserError('boom'),
        ):
            order._meli_flag_status_change({'id': 'FIVT-PACKF-B', 'status': 'cancelled'})

        self.assertEqual(order.state, 'sale')
        self.assertEqual(len(order.picking_ids), picking_count_before)
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(line_b.product_uom_qty, 1)
        self.assertTrue(any(
            'review manually' in (msg.body or '').lower() for msg in order.message_ids
        ))

    # ---- Fix Round 1 (task-6-report.md) ------------------------------

    def test_credit_note_document_arriving_after_partial_cancellation_does_not_over_refund_via_upsert(self):
        """Critical fix: ML's own credit-note document for the cancelled
        sibling can genuinely arrive AFTER
        _meli_process_partial_cancellation already ran and found
        nothing to relate — a completely ordinary ordering, since the
        physical return can easily complete before the devolución CFDI
        does (exactly what
        test_partial_cancellation_only_touches_the_cancelled_siblings_line
        already models). Before this fix,
        meli.invoice.document._meli_upsert's own unconditional call to
        sale_order_id._meli_reconcile_invoicing() would fall through to
        the whole-invoice account.move.reversal path there and
        credit-note EVERY line of the shared invoice, wiping out
        sibling A's own still-legitimate revenue too. Calls the real
        _meli_upsert entry point (not just _meli_process_partial_
        cancellation directly) — the actual way a credit-note webhook
        reaches this code in production.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK5', 'FIVT-PACK5-A', 'ZTEST-FIVT5-A', 'FIVT-PACK5-B', 'ZTEST-FIVT5-B',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK5-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK5-B')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK5-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000071',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('940')),
        })
        order._meli_reconcile_invoicing()
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        invoice_line_a = invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product' and l.product_id == self.product
        )
        self.assertEqual(invoice_line_a.quantity, 1)

        # No credit-note document exists yet for sibling B — the
        # physical return is processed first.
        order._meli_process_partial_cancellation('FIVT-PACK5-B')
        self.assertFalse(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund'))
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(line_b.product_uom_qty, 0)

        # ONLY NOW does Mercado Libre's own devolución CFDI for sibling
        # B arrive, through the real webhook entry point.
        self.env['meli.invoice.document']._meli_upsert(
            'FIVT-PACK5-B', 'devolution', self._fake_cfdi_xml('941'),
            meli_invoice_id='9000000000000072',
        )

        credit_notes = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_notes), 1)
        credit_lines = credit_notes.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(len(credit_lines), 1, "must cover ONLY sibling B's own line")
        self.assertEqual(credit_lines.product_id, second_product)
        # The load-bearing assertion: sibling A's own line, quantity and
        # invoiced amount are COMPLETELY untouched — a whole-invoice
        # reversal (the pre-fix behaviour) would have credit-noted this
        # too.
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(
            invoice.invoice_line_ids.filtered(
                lambda l: l.display_type == 'product' and l.product_id == self.product
            ).quantity, 1,
        )

    def test_second_siblings_own_cancellation_is_not_swallowed_by_the_same_status_dedupe(self):
        """Important #1: meli_last_status is one field on the shared
        consolidated order — sibling B cancelling sets it to
        'cancelled'; sibling A cancelling AFTERWARD must still be
        processed on its own, not silently dropped just because the
        raw status string happens to repeat.
        """
        order, _second_product = self._create_meli_full_pack(
            'FIVT-PACKD', 'FIVT-PACKD-A', 'ZTEST-FIVTD-A', 'FIVT-PACKD-B', 'ZTEST-FIVTD-B',
        )
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKD-A')
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKD-B')

        order._meli_flag_status_change({'id': 'FIVT-PACKD-B', 'status': 'cancelled'})
        self.assertEqual(order.meli_last_status, 'cancelled')
        self.assertEqual(line_b.product_uom_qty, 0)
        self.assertEqual(line_a.product_uom_qty, 1)

        # Sibling A cancels too, AFTER B — the raw status string
        # ('cancelled') is identical to what's already stored, but this
        # is a genuinely different, unprocessed event.
        order._meli_flag_status_change({'id': 'FIVT-PACKD-A', 'status': 'cancelled'})

        self.assertEqual(order.state, 'sale')
        self.assertEqual(
            line_a.product_uom_qty, 0,
            "sibling A's own cancellation must not be swallowed by the dedupe",
        )
        returns = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'incoming')
        self.assertEqual(len(returns), 2, "one return transfer per sibling")

    def test_partial_cancellation_routes_pack_order_even_when_notified_sibling_has_no_lines(self):
        """Important #2: _meli_add_pack_sibling_lines can leave a
        sibling with ZERO lines when every one of its SKUs is unmapped
        — that sibling's id never appears among this order's own
        order_line.meli_order_id values. Keying the routing guard off
        sibling_ids/order lines (the original shape) let a cancellation
        notification for that sibling fall through into the whole-order
        `is_full` cancellation branch: the ENTIRE sale got cancelled and
        the OTHER, still-paid sibling's stock got returned — backwards
        from what was actually reported. Keying off self.meli_pack_id
        instead must route this to the (safely no-op)
        partial-cancellation path and leave the sale and sibling A
        alone.
        """
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-FIVTN-A',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_a = {
            'id': 'FIVT-PACKN-A', 'status': 'paid', 'pack_id': 'FIVT-PACKN',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-NA', 'seller_sku': 'ZTEST-FIVTN-A'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        # Sibling B's own SKU is deliberately never mapped — no line at
        # all gets added for it (see _meli_add_pack_sibling_lines's own
        # docstring), so 'FIVT-PACKN-B' never appears among this order's
        # own order_line.meli_order_id values.
        order_data_b = {
            'id': 'FIVT-PACKN-B', 'status': 'paid', 'pack_id': 'FIVT-PACKN',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-NB', 'seller_sku': 'ZTEST-FIVTN-UNMAPPED'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        order.warehouse_id = self.warehouse_fulfillment.id
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKN-A')
        self.assertEqual(
            len(order.order_line), 1,
            "sibling B's unmapped SKU must have added no line at all",
        )

        order._meli_flag_status_change({'id': 'FIVT-PACKN-B', 'status': 'cancelled'})

        self.assertEqual(order.state, 'sale', "the whole sale must NOT be cancelled")
        self.assertEqual(line_a.product_uom_qty, 1, "sibling A's own line must be untouched")
        returns = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'incoming')
        self.assertFalse(returns, "sibling A's own stock must NOT have been returned")

    def test_partial_cancellation_when_sibling_was_never_delivered_zeroes_the_quantity_without_claiming_a_return(self):
        """Important #3: if the sibling's own outbound picking never
        validated in the first place (e.g. Full auto-validation failed
        earlier for insufficient stock), there is nothing to return —
        but the line must still reflect that nothing was (or ever will
        be) actually fulfilled for this now-cancelled sibling, not
        silently keep its full original ordered quantity forever
        alongside a still-live outbound move. The chatter must not
        claim stock was returned when none was.
        """
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-FIVTU-A',
        })
        second_product = self.env['product.product'].create({
            'name': 'Producto Invoicing Test (undelivered)', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': 'ZTEST-FIVTU-B',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        # Deliberately NO stock provisioned for the second product — its
        # own outbound move is left unreserved/undelivered, matching the
        # real "Full auto-validation failed for insufficient stock"
        # shape.
        order_data_a = {
            'id': 'FIVT-PACKU-A', 'status': 'paid', 'pack_id': 'FIVT-PACKU',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-UA', 'seller_sku': 'ZTEST-FIVTU-A'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order_data_b = {
            'id': 'FIVT-PACKU-B', 'status': 'paid', 'pack_id': 'FIVT-PACKU',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-UB', 'seller_sku': 'ZTEST-FIVTU-B'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        # Sibling A is imported AND delivered FIRST, before sibling B
        # ever arrives — its own outbound picking is already 'done' by
        # the time B's line gets added, which is what forces Odoo's own
        # stock rules to create a genuinely SEPARATE picking for B
        # (confirmed in practice: adding B's line while A's picking was
        # still undone instead merges both moves into that SAME
        # picking, which would make this scenario impossible to set up
        # — there'd be nothing to "only validate A's own transfer").
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKU-A')
        picking_a = order.picking_ids
        picking_a.action_assign()
        picking_a.button_validate()

        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKU-B')
        picking_b = order.picking_ids - picking_a
        self.assertTrue(picking_b)
        self.assertNotEqual(picking_b.state, 'done', "sibling B's own transfer was never validated")

        order._meli_process_partial_cancellation('FIVT-PACKU-B')

        self.assertEqual(order.state, 'sale')
        self.assertEqual(line_a.product_uom_qty, 1)
        self.assertEqual(line_b.product_uom_qty, 0, "must reflect nothing was ever delivered")
        returns = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'incoming')
        self.assertFalse(returns, "nothing was ever delivered, so nothing should have been returned")
        messages = [msg.body or '' for msg in order.message_ids if 'FIVT-PACKU-B' in (msg.body or '')]
        self.assertTrue(messages)
        self.assertFalse(
            any('returned' in body.lower() for body in messages),
            "must not claim stock was returned when none was",
        )

    def test_partial_cancellation_second_distinct_credit_note_document_does_not_double_refund(self):
        """Final review Fix 1 (Critical, sibling-scoped side): same
        double-refund hazard as
        test_reconcile_second_credit_note_document_does_not_double_refund
        above, but through _meli_relate_partial_cancellation_credit_note
        — the sibling-scoped credit-note builder
        _meli_process_partial_cancellation uses. A genuinely different,
        SECOND devolución document for the SAME cancelled sibling (not a
        re-processed upsert of the identical document — see
        test_partial_cancellation_second_credit_note_document_upsert_does_not_double_refund
        above for that idempotent-replay case, which this must not
        regress) must never create a second out_refund covering that
        sibling's own lines.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK6', 'FIVT-PACK6-A', 'ZTEST-FIVT6-A', 'FIVT-PACK6-B', 'ZTEST-FIVT6-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK6-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000120',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('990')),
        })
        order._meli_reconcile_invoicing()
        first_credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK6-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000121',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('991')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK6-B')

        first_refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(first_refunds), 1)
        self.assertEqual(first_refunds.meli_invoice_document_id, first_credit_note_document)

        # Mercado Libre issues a REPLACEMENT devolución for sibling B's
        # own cancellation — a genuinely different document row.
        second_credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK6-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000122',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('992')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK6-B')

        refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(refunds), 1, "must never create a SECOND, duplicate credit note")
        self.assertEqual(refunds, first_refunds)
        self.assertTrue(any(
            second_credit_note_document.meli_invoice_id in (msg.body or '')
            and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    # ---- 2026-09-09 follow-up fixes (task-8) --------------------------

    def test_partial_cancellation_of_last_sibling_auto_cancels_the_sale(self):
        """Fix 1: once EVERY sibling in an active Full pack has been
        individually cancelled — its own line at qty 0 AND its own
        credit note already related, see
        sale.order._meli_sibling_is_fully_cancelled — the sale itself
        must be closed too. Before this fix nothing ever did that:
        _meli_process_partial_cancellation only ever closed out the ONE
        sibling it was called for, no matter how many siblings had
        already been individually cancelled before it.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK8', 'FIVT-PACK8-A', 'ZTEST-FIVT8-A', 'FIVT-PACK8-B', 'ZTEST-FIVT8-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK8-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000150',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1020')),
        })
        order._meli_reconcile_invoicing()
        self.assertTrue(order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ))
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK8-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000151',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1021')),
        })
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK8-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000152',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1022')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK8-A')
        self.assertEqual(order.state, 'sale', "only sibling A is cancelled so far")

        order._meli_process_partial_cancellation('FIVT-PACK8-B')

        self.assertEqual(order.state, 'cancel', "every sibling is now cancelled")
        self.assertTrue(any(
            'every individual order within this pack' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_partial_cancellation_of_last_of_three_siblings_auto_cancels_the_sale(self):
        """Fix 1, 3-sibling variant: cancelling 2 of 3 siblings must
        leave the sale open; only the THIRD and final cancellation
        closes it.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK9', 'FIVT-PACK9-A', 'ZTEST-FIVT9-A', 'FIVT-PACK9-B', 'ZTEST-FIVT9-B',
        )
        third_product = self.env['product.product'].create({
            'name': 'Producto Invoicing Test (FIVT9-C)', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': third_product.id, 'meli_sku': 'ZTEST-FIVT9-C',
        })
        # warehouse_fulfillment, not warehouse_default: _create_meli_full_pack
        # already forced order.warehouse_id to warehouse_fulfillment before
        # this sibling's own line gets added below, so its own new stock
        # move needs to reserve from THAT warehouse's own stock.
        self.env['stock.quant']._update_available_quantity(
            third_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order_data_c = {
            'id': 'FIVT-PACK9-C', 'status': 'paid', 'pack_id': 'FIVT-PACK9',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-C', 'seller_sku': 'ZTEST-FIVT9-C'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        pickings_before_c = order.picking_ids
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_c)
        # Both siblings A and B were already fully delivered (via
        # _create_meli_full_pack) by the time sibling C's own line is
        # added — same reasoning as this class's own _create_meli_pack
        # docstring: adding a line onto an order whose existing
        # pickings are already 'done' forces a genuinely SEPARATE new
        # picking for it, so only that new one needs assigning/
        # validating here.
        new_picking = order.picking_ids - pickings_before_c
        self.assertTrue(new_picking)
        new_picking.action_assign()
        new_picking.button_validate()

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK9-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000160',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1030')),
        })
        order._meli_reconcile_invoicing()
        self.assertTrue(order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ))
        for sibling_id, meli_invoice_id, folio in (
            ('FIVT-PACK9-A', '9000000000000161', '1031'),
            ('FIVT-PACK9-B', '9000000000000162', '1032'),
            ('FIVT-PACK9-C', '9000000000000163', '1033'),
        ):
            self.env['meli.invoice.document'].sudo().create({
                'meli_order_id': sibling_id, 'transaction_type': 'devolution',
                'meli_invoice_id': meli_invoice_id,
                'xml_file': base64.b64encode(self._fake_cfdi_xml(folio)),
            })

        order._meli_process_partial_cancellation('FIVT-PACK9-A')
        self.assertEqual(order.state, 'sale')

        order._meli_process_partial_cancellation('FIVT-PACK9-B')
        self.assertEqual(order.state, 'sale', "sibling C is still active")

        order._meli_process_partial_cancellation('FIVT-PACK9-C')

        self.assertEqual(order.state, 'cancel', "every one of the three siblings is now cancelled")

    def test_zero_line_sibling_cancellation_notifies_queue_job_managers(self):
        """Fix 2: before this fix, a cancelled sibling with zero
        resolvable lines (every one of its SKUs unmapped) made
        _meli_process_partial_cancellation return completely silently —
        no chatter, no log, nothing for a human to ever see. This
        proves the fix posts a chatter message AND routes it, as a
        direct one-off notification (never a permanent follower), to
        whoever holds the real 'Queue Job Manager' group — the exact
        same group/mechanism queue_job itself already uses for its own
        failed-job notifications. queue_job is a hard dependency of
        this module (see its own __manifest__.py 'depends'), so the
        group is always resolvable in this test environment.
        """
        manager_group = self.env.ref('queue_job.group_queue_job_manager')
        manager_user = self.env['res.users'].create({
            'name': 'Queue Job Manager Test User',
            'login': 'meli_queue_job_manager_test_user',
            'email': 'meli_queue_job_manager_test_user@example.com',
            'company_id': self.test_company.id,
            'company_ids': [(6, 0, [self.test_company.id])],
            'groups_id': [(4, manager_group.id)],
        })
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-FIVTZ-A',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_a = {
            'id': 'FIVT-PACKZ-A', 'status': 'paid', 'pack_id': 'FIVT-PACKZ',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-ZA', 'seller_sku': 'ZTEST-FIVTZ-A'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        # Sibling B's own SKU is deliberately never mapped — same shape
        # as test_partial_cancellation_routes_pack_order_even_when_
        # notified_sibling_has_no_lines above.
        order_data_b = {
            'id': 'FIVT-PACKZ-B', 'status': 'paid', 'pack_id': 'FIVT-PACKZ',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-ZB', 'seller_sku': 'ZTEST-FIVTZ-UNMAPPED'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        order.warehouse_id = self.warehouse_fulfillment.id
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()

        order._meli_process_partial_cancellation('FIVT-PACKZ-B')

        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn('FIVT-PACKZ-B', message.body)
        self.assertIn('sku', message.body.lower())
        self.assertIn(manager_user.partner_id, message.partner_ids)
        self.assertNotIn(
            manager_user.partner_id, order.message_follower_ids.mapped('partner_id'),
            "must be a direct one-off notification, never a permanent follower",
        )

    def _create_queue_job_manager(self, login):
        manager_group = self.env.ref('queue_job.group_queue_job_manager')
        return self.env['res.users'].create({
            'name': 'Queue Job Manager Test User (%s)' % login,
            'login': login, 'email': '%s@example.com' % login,
            'company_id': self.test_company.id,
            'company_ids': [(6, 0, [self.test_company.id])],
            'groups_id': [(4, manager_group.id)],
        })

    def test_partial_cancellation_failure_notifies_queue_job_managers(self):
        manager_user = self._create_queue_job_manager('meli_qjm_partial_fail')
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACKQJM1', 'FIVT-PACKQJM1-A', 'ZTEST-QJM1-A',
            'FIVT-PACKQJM1-B', 'ZTEST-QJM1-B',
        )

        with patch.object(
            type(order), '_meli_process_partial_cancellation',
            side_effect=UserError('boom'),
        ):
            order._meli_flag_status_change({'status': 'cancelled', 'id': 'FIVT-PACKQJM1-B'})

        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn('FIVT-PACKQJM1-B', message.body)
        self.assertIn(manager_user.partner_id, message.partner_ids)
        self.assertNotIn(
            manager_user.partner_id, order.message_follower_ids.mapped('partner_id'),
            "must be a direct one-off notification, never a permanent follower",
        )

    def test_full_cancellation_phase1_failure_notifies_queue_job_managers(self):
        manager_user = self._create_queue_job_manager('meli_qjm_phase1_fail')
        order = self._create_delivered_order('FIVT-QJM2')

        with patch.object(
            type(order), '_meli_process_full_cancellation',
            side_effect=UserError('boom'),
        ):
            order._meli_flag_status_change({'status': 'cancelled'})

        self.assertEqual(order.state, 'sale')
        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn('cancelled', message.body.lower())
        self.assertIn('failed', message.body.lower())
        self.assertIn(manager_user.partner_id, message.partner_ids)
        self.assertNotIn(
            manager_user.partner_id, order.message_follower_ids.mapped('partner_id'),
        )
        # Fix 2's own behavior change: the specific failure message
        # above must be the ONLY one posted — the old code additionally
        # fell through to the generic "changed to status X" message,
        # which this fix's return now prevents.
        self.assertEqual(
            len(order.message_ids.filtered(
                lambda m: 'changed to status' in (m.body or '').lower()
            )),
            0,
            "the generic fallback message must not also fire",
        )

    def test_full_cancellation_success_notifies_queue_job_managers(self):
        manager_user = self._create_queue_job_manager('meli_qjm_success')
        order = self._create_delivered_order('FIVT-QJM3')

        order._meli_flag_status_change({'status': 'cancelled'})

        self.assertEqual(order.state, 'cancel')
        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn('Handled automatically', message.body)
        self.assertIn(manager_user.partner_id, message.partner_ids)

    def test_cancelled_on_arrival_phase1_failure_notifies_queue_job_managers(self):
        manager_user = self._create_queue_job_manager('meli_qjm_recovery_fail')
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-QJM4',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-QJM4', 'transaction_type': 'sale',
            'meli_invoice_id': '9300000000000001',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('900')),
        })
        order_data = {
            'id': 'FIVT-QJM4', 'status': 'cancelled', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-QJM4', 'seller_sku': 'ZTEST-QJM4'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }

        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['sale.order']), '_meli_process_full_cancellation',
            side_effect=UserError('boom'),
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.state, 'sale')
        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn(manager_user.partner_id, message.partner_ids)

    def test_cancelled_on_arrival_pack_skip_notifies_queue_job_managers(self):
        manager_user = self._create_queue_job_manager('meli_qjm_pack_skip')
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-QJM5',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-QJM5-PACK', 'transaction_type': 'sale',
            'meli_invoice_id': '9300000000000002',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('901')),
        })
        order_data = {
            'id': 'FIVT-QJM5', 'status': 'cancelled', 'pack_id': 'FIVT-QJM5-PACK',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-QJM5', 'seller_sku': 'ZTEST-QJM5'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }

        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.state, 'sale')
        message = order.message_ids.sorted('id', reverse=True)[0]
        self.assertIn('pack', message.body.lower())
        self.assertIn(manager_user.partner_id, message.partner_ids)

    def test_reconcile_refactura_skips_when_order_has_a_live_partial_cancellation_credit_note(self):
        """Fix 3: refacturación (cancel + recreate the Odoo invoice) must
        never proceed automatically while the order already has a live
        (non-cancelled) credit note from a prior partial cancellation —
        cancelling the invoice that credit note reverses would corrupt
        that relationship. Before this fix, a NEW/replacement 'sale'
        document for the still-active sibling would have cancelled the
        shared invoice and created a replacement, orphaning the
        already-posted credit note's own reversed_entry_id.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK7', 'FIVT-PACK7-A', 'ZTEST-FIVT7-A', 'FIVT-PACK7-B', 'ZTEST-FIVT7-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK7-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000130',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1000')),
        })
        order._meli_reconcile_invoicing()
        original_invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(original_invoice.state, 'posted')

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK7-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000131',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1001')),
        })
        order._meli_process_partial_cancellation('FIVT-PACK7-B')
        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(credit_note.state, 'posted')

        # Mercado Libre now issues a NEW/replacement 'sale' document for
        # sibling A — an ordinary refacturación trigger under any other
        # circumstance.
        new_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK7-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000132',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1002')),
        })

        order._meli_reconcile_invoicing()

        self.assertEqual(
            original_invoice.state, 'posted', "the old invoice must NOT have been cancelled",
        )
        self.assertEqual(
            len(order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')), 1,
            "no replacement invoice should have been created",
        )
        self.assertEqual(
            len(order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')), 1,
            "the existing credit note must be left exactly as it was",
        )
        self.assertTrue(any(
            new_document.meli_invoice_id in (msg.body or '')
            and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_reconcile_credit_note_uses_document_issue_date_not_today_or_invoice_date(self):
        """Fix 4, whole-order account.move.reversal path: the credit
        note's own invoice_date must come from the REAL Mercado Libre
        document's own issue_date (the CFDI's own Fecha attribute) —
        never today (the wizard's own default) and never the source
        invoice's own date (which, since it was posted without an
        explicit invoice_date, also defaults to today — a second,
        independently wrong answer this fix must avoid too).

        The credit note document's Fecha below (23:45 Monterrey time)
        deliberately rolls into the NEXT calendar day once converted to
        UTC for storage (see meli.invoice.document.
        _meli_parse_issue_date_from_xml) — the sanity check below
        confirms that shift really happened, proving this test actually
        exercises the Monterrey-timezone conversion
        _meli_credit_note_invoice_date performs, not just a
        pass-through of an already-matching date.
        """
        order = self._create_delivered_order('FIVT-0060')
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0060', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000170',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1040')),
        })
        order._meli_reconcile_invoicing()
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(invoice.state, 'posted')

        # issue_date is only ever populated by meli.invoice.document.
        # _meli_upsert's own XML parsing (_meli_parse_issue_date_from_xml)
        # — a plain .create() call (used throughout this file, same as
        # every other fixture above) never parses the XML at all, so it's
        # set explicitly here, computed the exact same way the real
        # parser would (Monterrey-local Fecha converted to naive UTC for
        # storage) — that XML->issue_date conversion itself is already
        # covered by test_meli_invoice_document.py's own tests; what's
        # under test here is sale_order.py reading this field back
        # correctly, not meli_invoice_document.py parsing it.
        credit_note_issue_date = MELI_FISCAL_TIMEZONE.localize(
            datetime(2026, 9, 8, 23, 45, 0)
        ).astimezone(pytz.utc).replace(tzinfo=None)
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-0060', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000171',
            'xml_file': base64.b64encode(
                self._fake_cfdi_xml('1041', fecha='2026-09-08T23:45:00')
            ),
            'issue_date': credit_note_issue_date,
        })
        self.assertNotEqual(
            credit_note_document.issue_date.date(), date(2026, 9, 8),
            "sanity check: issue_date must already be stored one UTC day "
            "later than the CFDI's own Monterrey-local Fecha",
        )

        order._meli_reconcile_invoicing()

        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.invoice_date, date(2026, 9, 8))
        self.assertNotEqual(credit_note.invoice_date, fields.Date.today())
        self.assertNotEqual(credit_note.invoice_date, invoice.invoice_date)

    def test_partial_cancellation_credit_note_uses_document_issue_date_not_today_or_invoice_date(self):
        """Fix 4, sibling-scoped hand-built path
        (_meli_relate_partial_cancellation_credit_note): same fix as
        the whole-order path above, but for the credit note
        _meli_process_partial_cancellation builds by hand — this one
        used to copy source_invoice's own invoice_date outright, never
        the real Mercado Libre document's own date.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK10', 'FIVT-PACK10-A', 'ZTEST-FIVT10-A', 'FIVT-PACK10-B', 'ZTEST-FIVT10-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK10-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000180',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1050')),
        })
        order._meli_reconcile_invoicing()
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(invoice.state, 'posted')

        # See the equivalent comment in
        # test_reconcile_credit_note_uses_document_issue_date_not_today_or_invoice_date
        # above for why issue_date is set explicitly here.
        credit_note_issue_date = MELI_FISCAL_TIMEZONE.localize(
            datetime(2026, 9, 8, 23, 45, 0)
        ).astimezone(pytz.utc).replace(tzinfo=None)
        credit_note_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK10-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000181',
            'xml_file': base64.b64encode(
                self._fake_cfdi_xml('1051', fecha='2026-09-08T23:45:00')
            ),
            'issue_date': credit_note_issue_date,
        })
        self.assertNotEqual(
            credit_note_document.issue_date.date(), date(2026, 9, 8),
            "sanity check: issue_date must already be stored one UTC day "
            "later than the CFDI's own Monterrey-local Fecha",
        )

        order._meli_process_partial_cancellation('FIVT-PACK10-B')

        credit_note = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.invoice_date, date(2026, 9, 8))
        self.assertNotEqual(credit_note.invoice_date, fields.Date.today())
        self.assertNotEqual(credit_note.invoice_date, invoice.invoice_date)

    # ---- 2026-09-09 fix round 1 (reviewer findings, task-8) -----------

    def test_pack_closure_failure_does_not_roll_back_the_siblings_own_cancellation(self):
        """Fix round 1, Fix A: if the final "close the sale" step fails
        (action_unlock()/action_cancel() can genuinely raise — see
        _meli_process_full_cancellation's own comment on
        action_unlock()), the LAST sibling's own already-successful
        credit note / stock return / quantity update — completed
        earlier in this SAME call — must NOT be rolled back just
        because the final state transition failed. Before this fix,
        _meli_close_pack_if_every_sibling_cancelled() shared the outer
        savepoint _meli_flag_status_change sets up around the whole
        partial-cancellation call, so an exception here would have
        discarded that already-successful work too.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK11', 'FIVT-PACK11-A', 'ZTEST-FIVT11-A', 'FIVT-PACK11-B', 'ZTEST-FIVT11-B',
        )
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK11-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000190',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1060')),
        })
        order._meli_reconcile_invoicing()
        self.assertTrue(order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ))
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK11-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000191',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1061')),
        })
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK11-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000192',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1062')),
        })
        order._meli_process_partial_cancellation('FIVT-PACK11-A')
        self.assertEqual(order.state, 'sale')

        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK11-B')
        with patch.object(type(order), 'action_cancel', side_effect=UserError('boom')):
            order._meli_process_partial_cancellation('FIVT-PACK11-B')

        self.assertEqual(
            order.state, 'sale',
            "the sale itself could not be closed, so it correctly stays open",
        )
        self.assertEqual(
            line_b.product_uom_qty, 0,
            "sibling B's own quantity update from THIS SAME call must survive",
        )
        credit_notes = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(
            len(credit_notes), 2,
            "both siblings' own credit notes (A's from before, B's from THIS call) must survive",
        )
        returns = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'incoming')
        self.assertEqual(len(returns), 2, "both siblings' own stock returns must survive")
        self.assertTrue(any(
            'could not be cancelled automatically' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_reconcile_refactura_block_does_not_prevent_step_3_for_another_sibling(self):
        """Fix round 1, Fix B: the refacturación guard must only skip
        the invoice-creation/refacturación step for THIS call, never
        the whole method — Step 3 (credit-note relating) is a
        genuinely separate reconciliation that can concern a completely
        different sibling and must still run in the SAME
        _meli_reconcile_invoicing() call. Before this fix, the guard's
        own `return` skipped Step 3 entirely, and — since this method
        is idempotent and re-entered on every picking completion /
        webhook / document upsert — would have kept blocking Step 3 on
        every future call for this order too, not just this one.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK12', 'FIVT-PACK12-A', 'ZTEST-FIVT12-A', 'FIVT-PACK12-B', 'ZTEST-FIVT12-B',
        )
        third_product = self.env['product.product'].create({
            'name': 'Producto Invoicing Test (FIVT12-C)', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': third_product.id, 'meli_sku': 'ZTEST-FIVT12-C',
        })
        self.env['stock.quant']._update_available_quantity(
            third_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order_data_c = {
            'id': 'FIVT-PACK12-C', 'status': 'paid', 'pack_id': 'FIVT-PACK12',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-C', 'seller_sku': 'ZTEST-FIVT12-C'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        pickings_before_c = order.picking_ids
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_c)
        new_picking = order.picking_ids - pickings_before_c
        self.assertTrue(new_picking)
        new_picking.action_assign()
        new_picking.button_validate()
        line_c = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACK12-C')

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK12-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000200',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1070')),
        })
        order._meli_reconcile_invoicing()
        original_invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(original_invoice.state, 'posted')

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK12-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000201',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1071')),
        })
        order._meli_process_partial_cancellation('FIVT-PACK12-B')
        credit_note_b = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(len(credit_note_b), 1)

        # Refacturación trigger for sibling A (blocked by the live
        # credit note above) AND, in the SAME reconciler call, a
        # genuinely actionable credit-note document for sibling C — a
        # completely separate reconciliation Step 3 must still perform.
        new_sale_document = self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK12-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000202',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1072')),
        })
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK12-C', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000203',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1073')),
        })

        order._meli_reconcile_invoicing()

        self.assertEqual(
            original_invoice.state, 'posted', "refacturación must still be blocked",
        )
        self.assertEqual(
            len(order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')), 1,
            "no replacement invoice should have been created",
        )
        self.assertTrue(any(
            new_sale_document.meli_invoice_id in (msg.body or '')
            and 'review manually' in (msg.body or '').lower()
            for msg in order.message_ids
        ))
        refunds = order.invoice_ids.filtered(lambda m: m.move_type == 'out_refund')
        self.assertEqual(
            len(refunds), 2,
            "sibling C's own credit note must have been related too, in the SAME call",
        )
        credit_note_c = refunds - credit_note_b
        credit_lines_c = credit_note_c.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(credit_lines_c.mapped('product_id'), third_product)
        # Step 3 (credit-note relating) only ever touches the fiscal
        # side — it never adjusts stock or line quantity, that's
        # _meli_process_partial_cancellation's own job (not exercised
        # here, this call went straight through _meli_reconcile_
        # invoicing() instead) — so sibling C's own line is expected to
        # still read its full, untouched quantity.
        self.assertEqual(line_c.product_uom_qty, 1)

    def test_pack_with_an_unmapped_sibling_cancellation_does_not_auto_close(self):
        """Fix round 1, Fix C: a pack where one sibling (A) is mapped and
        gets fully, individually cancelled (own line at qty 0, own
        credit note related) must NOT auto-cancel the sale if ANOTHER
        sibling (this pack's own unmapped one, B) also reported a
        cancellation with zero resolvable lines of its own — B's real
        status on Mercado Libre can never be confirmed from Odoo's
        side (see meli_pack_has_unmapped_sibling_cancellation's own
        help text), so the sale must be left open for manual review
        instead of auto-cancelling just because every VISIBLE sibling
        happens to be done.
        """
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-FIVTU2-A',
        })
        # warehouse_default, not warehouse_fulfillment: both siblings'
        # own lines get added while the order is still routed onto
        # warehouse_default (shipping={} — same as _create_meli_pack's
        # own docstring explains); warehouse_id only gets forced onto
        # warehouse_fulfillment AFTER both lines already exist, which
        # doesn't retroactively move the picking(s) already created
        # against warehouse_default's own locations.
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_a = {
            'id': 'FIVT-PACKU2-A', 'status': 'paid', 'pack_id': 'FIVT-PACKU2',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-U2A', 'seller_sku': 'ZTEST-FIVTU2-A'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        # Sibling B's own SKU is deliberately never mapped — no line at
        # all gets added for it, same shape as
        # test_partial_cancellation_routes_pack_order_even_when_
        # notified_sibling_has_no_lines above.
        order_data_b = {
            'id': 'FIVT-PACKU2-B', 'status': 'paid', 'pack_id': 'FIVT-PACKU2',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-U2B', 'seller_sku': 'ZTEST-FIVTU2-UNMAPPED'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        order.warehouse_id = self.warehouse_fulfillment.id
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKU2-A')
        self.assertEqual(
            len(order.order_line), 1, "sibling B's unmapped SKU must have added no line at all",
        )

        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACKU2-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000210',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1080')),
        })
        order._meli_reconcile_invoicing()
        self.assertTrue(order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ))
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACKU2-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000211',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1081')),
        })

        # The unmapped sibling B reports 'cancelled' FIRST — sets the
        # flag, but has nothing of its own to touch.
        order._meli_flag_status_change({'id': 'FIVT-PACKU2-B', 'status': 'cancelled'})
        self.assertTrue(order.meli_pack_has_unmapped_sibling_cancellation)

        # Now sibling A — the only sibling with a resolvable line — gets
        # fully, individually cancelled too. Without Fix C this would
        # have auto-cancelled the whole sale.
        order._meli_process_partial_cancellation('FIVT-PACKU2-A')

        self.assertEqual(
            order.state, 'sale',
            "the sale must stay open — sibling B's real status was never confirmed",
        )
        self.assertEqual(line_a.product_uom_qty, 0)
        self.assertTrue(any(
            'not cancelled automatically' in (msg.body or '').lower()
            for msg in order.message_ids
        ))

    def test_pack_closure_still_happens_when_no_unmapped_sibling_was_ever_seen(self):
        """Fix round 1, Fix C regression guard: an ordinary pack where
        every sibling genuinely has its own line (no unmapped SKU ever
        involved) must still auto-cancel exactly as Fix 1 established —
        meli_pack_has_unmapped_sibling_cancellation must default False
        and never block this ordinary case.
        """
        order, second_product = self._create_meli_full_pack(
            'FIVT-PACK13', 'FIVT-PACK13-A', 'ZTEST-FIVT13-A', 'FIVT-PACK13-B', 'ZTEST-FIVT13-B',
        )
        self.assertFalse(order.meli_pack_has_unmapped_sibling_cancellation)
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK13-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000220',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1090')),
        })
        order._meli_reconcile_invoicing()
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK13-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000221',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1091')),
        })
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACK13-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000222',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1092')),
        })

        order._meli_process_partial_cancellation('FIVT-PACK13-A')
        self.assertEqual(order.state, 'sale')
        order._meli_process_partial_cancellation('FIVT-PACK13-B')

        self.assertEqual(order.state, 'cancel')

    def test_unmapped_sibling_flag_clears_once_mapped_and_pack_then_auto_closes(self):
        """Fix 2026-09-09 (user-directed follow-up), full round-trip:
        meli_pack_has_unmapped_sibling_cancellation must not stay True
        forever just because the sibling that once triggered it was
        unmapped at the time — once that same sibling's SKU is mapped
        and its own line is genuinely added via
        _meli_add_pack_sibling_lines (the module's own documented
        recovery path — see that method's own docstring, "SKU was
        unmapped, now it's mapped, re-run the import"), the flag must
        clear automatically and the pack must be able to auto-close
        normally afterward, without any developer-mode technical field
        edit. Same starting shape as
        test_pack_with_an_unmapped_sibling_cancellation_does_not_auto_close
        (sibling B starts genuinely unmapped, zero lines, and cancels
        while unmapped — setting the flag True); this test continues
        past that point instead of stopping there.

        Invoicing is deliberately deferred until AFTER sibling B's own
        line is added (rather than mirroring the cited test's exact
        step order, which invoices sibling A alone first): a single,
        shared invoice covering BOTH siblings' lines — the real,
        confirmed production shape _meli_relate_partial_cancellation_
        credit_note's own docstring documents — is what lets sibling
        B's own credit note be related unambiguously afterward,
        without depending on which of two separate posted invoices
        happens to be picked first.
        """
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-FIVTRT-A',
        })
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_default.lot_stock_id, 10,
        )
        order_data_a = {
            'id': 'FIVT-PACKRT-A', 'status': 'paid', 'pack_id': 'FIVT-PACKRT',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-RT-A', 'seller_sku': 'ZTEST-FIVTRT-A'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        # Sibling B's own SKU is deliberately never mapped at first —
        # no line at all gets added for it, same shape as
        # test_pack_with_an_unmapped_sibling_cancellation_does_not_auto_close.
        order_data_b = {
            'id': 'FIVT-PACKRT-B', 'status': 'paid', 'pack_id': 'FIVT-PACKRT',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-RT-B', 'seller_sku': 'ZTEST-FIVTRT-B'},
                'quantity': 1, 'unit_price': 100.0,
            }],
        }
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data_a)
        self.env['sale.order']._meli_create_from_order_data(self.config, order_data_b)
        order.warehouse_id = self.warehouse_fulfillment.id
        order.picking_ids.action_assign()
        order.picking_ids.button_validate()
        line_a = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKRT-A')
        self.assertEqual(
            len(order.order_line), 1, "sibling B's unmapped SKU must have added no line at all",
        )

        # Sibling B reports 'cancelled' while still unmapped — sets the
        # flag, matching the existing coverage in
        # test_pack_with_an_unmapped_sibling_cancellation_does_not_auto_close.
        order._meli_flag_status_change({'id': 'FIVT-PACKRT-B', 'status': 'cancelled'})
        self.assertTrue(order.meli_pack_has_unmapped_sibling_cancellation)

        # Now sibling B's SKU finally gets mapped, and its own line is
        # added via the module's own documented recovery path —
        # simulating a person re-importing this specific order after
        # fixing the mapping. Nothing has been invoiced yet, so this is
        # still an ordinary "extra line on a not-yet-invoiced order"
        # case (_meli_add_pack_sibling_lines's own docstring), not the
        # "invoice already posted" one.
        second_product = self.env['product.product'].create({
            'name': 'Producto Round Trip Test (B)', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': 'ZTEST-FIVTRT-B',
        })
        self.env['stock.quant']._update_available_quantity(
            second_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order._meli_add_pack_sibling_lines(order_data_b, 'FIVT-PACKRT-B')

        line_b = order.order_line.filtered(lambda l: l.meli_order_id == 'FIVT-PACKRT-B')
        self.assertTrue(line_b, "sibling B must now have its own line")
        self.assertFalse(
            order.meli_pack_has_unmapped_sibling_cancellation,
            "the flag must clear once the once-unmapped sibling "
            "finally got its own line",
        )
        order.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
        ).action_assign()
        order.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
        ).button_validate()

        # Both siblings now have their own real line — invoice the
        # whole order in ONE shared invoice, exactly the confirmed
        # production shape (one consolidated pack invoice covering
        # every sibling), then relate each sibling's own credit note.
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACKRT-A', 'transaction_type': 'sale',
            'meli_invoice_id': '9000000000000240',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1200')),
        })
        order._meli_reconcile_invoicing()
        self.assertTrue(order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        ))
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACKRT-A', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000241',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1201')),
        })
        self.env['meli.invoice.document'].sudo().create({
            'meli_order_id': 'FIVT-PACKRT-B', 'transaction_type': 'devolution',
            'meli_invoice_id': '9000000000000242',
            'xml_file': base64.b64encode(self._fake_cfdi_xml('1202')),
        })

        # Both siblings now go through the completely ordinary
        # partial-cancellation flow, like any other sibling with a
        # real line — no special-casing needed anymore for either one.
        order._meli_process_partial_cancellation('FIVT-PACKRT-A')
        self.assertEqual(order.state, 'sale')
        order._meli_process_partial_cancellation('FIVT-PACKRT-B')

        self.assertEqual(
            order.state, 'cancel',
            "once every sibling (A from before, B now) is genuinely "
            "cancelled, the pack must auto-close normally — the flag "
            "no longer blocks it",
        )
        self.assertEqual(line_a.product_uom_qty, 0)
        self.assertEqual(line_b.product_uom_qty, 0)

    def test_ventiapp_order_is_adopted_not_duplicated(self):
        """A sale.order created by Ventiapp (no meli_sync_source, but the
        same reference this connector itself would use) must be adopted —
        only this connector's own control fields written — never
        duplicated into a second sale.order for the same real transaction.
        """
        ventiapp_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-1',
            'client_order_ref': 'FIVT-VENTIAPP-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 2,
            })],
        })
        self.assertFalse(ventiapp_order.meli_sync_source)

        order_data = {
            'id': 'FIVT-VENTIAPP-1', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': '2026-09-01T10:00:00.000-06:00',
            'date_closed': '2026-09-01T10:05:00.000-06:00',
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertEqual(result, ventiapp_order)
        self.assertEqual(
            self.env['sale.order'].search_count([
                ('reference', '=', 'FIVT-VENTIAPP-1'),
            ]),
            1,
            "adoption must not create a second sale.order",
        )
        self.assertEqual(ventiapp_order.meli_sync_source, 'xe_meli_connector')
        self.assertEqual(ventiapp_order.meli_order_id, 'FIVT-VENTIAPP-1')
        self.assertEqual(ventiapp_order.meli_last_status, 'paid')
        # Commercial fields Ventiapp already set must be untouched.
        self.assertEqual(ventiapp_order.origin, 'VENTIAPP-ML-XEBRANDS')
        self.assertEqual(ventiapp_order.warehouse_id, self.warehouse_default)
        self.assertEqual(len(ventiapp_order.order_line), 1)
        self.assertEqual(ventiapp_order.order_line.product_uom_qty, 2)
        self.assertTrue(any(
            'FIVT-VENTIAPP-1' in (msg.body or '')
            for msg in ventiapp_order.message_ids
        ))

    def test_ventiapp_adoption_only_matches_orders_this_connector_never_touched(self):
        """A sale.order that already has meli_sync_source set (i.e. one
        this connector itself created) must never be re-matched by the
        adoption lookup — that would defeat the earlier meli_order_id
        dedup and double-write control fields on an unrelated order that
        happens to share a reference value.
        """
        own_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-2',
            'client_order_ref': 'FIVT-VENTIAPP-2',
            'meli_sync_source': 'xe_meli_connector',
            'meli_order_id': 'FIVT-VENTIAPP-2-OTHER',
            'warehouse_id': self.warehouse_default.id,
        })
        order_data = {
            'id': 'FIVT-VENTIAPP-2', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )
        self.assertNotEqual(result, own_order)
        self.assertEqual(result.meli_order_id, 'FIVT-VENTIAPP-2')

    def test_adopted_draft_order_is_never_auto_confirmed_by_retry_unmapped_lines(self):
        """Fix 1 (2026-09-09, final review — Critical): the polling cron
        re-enqueues any draft order with meli_sync_source set, which
        eventually calls _meli_retry_unmapped_lines. An ADOPTED order
        genuinely sitting in draft (a not-yet-confirmed Ventiapp order)
        must never be auto-confirmed by that path — adoption may only
        ever update this connector's own control fields, never go on to
        automate anything else about an order this connector did not
        itself create.
        """
        ventiapp_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-DRAFT-1',
            'client_order_ref': 'FIVT-VENTIAPP-DRAFT-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'state': 'draft',
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        self.assertEqual(ventiapp_order.state, 'draft')

        order_data = {
            'id': 'FIVT-VENTIAPP-DRAFT-1', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertEqual(result, ventiapp_order)
        self.assertTrue(ventiapp_order.meli_adopted)
        self.assertEqual(ventiapp_order.state, 'draft')

        with patch.object(
            type(ventiapp_order), 'action_confirm',
        ) as mocked_confirm:
            ventiapp_order._meli_retry_unmapped_lines(order_data)

        mocked_confirm.assert_not_called()
        self.assertEqual(
            ventiapp_order.state, 'draft',
            "an adopted order must stay completely untouched by this path",
        )

    def test_adoption_via_a_non_paid_notification_still_processes_the_status_change(self):
        """Fix 2 (2026-09-09, final review — Important): the notification
        that triggers adoption isn't necessarily reporting 'paid' — e.g.
        a 'cancelled' notification for a Ventiapp order this connector
        never saw before. Adoption must still happen (control fields
        written) AND the actual status-change logic
        (_meli_flag_status_change) must still run for that same
        notification — reusing the exact assertion shape of
        test_non_full_order_cancelled_only_gets_the_manual_review_message
        above (a non-Full order with meli_sync_source set only ever gets
        the manual-review chatter message for a 'cancelled' status).
        Before this fix, the notification that triggered adoption was
        simply swallowed after recording meli_last_status.
        """
        ventiapp_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-CANCEL-1',
            'client_order_ref': 'FIVT-VENTIAPP-CANCEL-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        ventiapp_order.action_confirm()

        order_data = {
            'id': 'FIVT-VENTIAPP-CANCEL-1', 'status': 'cancelled', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertEqual(result, ventiapp_order)
        self.assertEqual(ventiapp_order.meli_sync_source, 'xe_meli_connector')
        self.assertEqual(ventiapp_order.meli_order_id, 'FIVT-VENTIAPP-CANCEL-1')
        self.assertEqual(
            ventiapp_order.meli_last_status, 'cancelled',
            "_meli_flag_status_change itself must have recorded this, "
            "not a pre-set value from the adoption write()",
        )
        self.assertTrue(any(
            'Review manually' in (msg.body or '') for msg in ventiapp_order.message_ids
        ), "the cancellation notification must have actually been processed")

    def test_cancelled_ventiapp_order_is_never_adopted(self):
        """Fix 3a (2026-09-09, final review — Important): an
        ALREADY-CANCELLED Ventiapp order sharing this reference must
        never be adopted — that would silently adopt the wrong, dead
        record and lose track of what should be a real, separate paid
        sale.
        """
        cancelled_ventiapp_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-CANCELLED-1',
            'client_order_ref': 'FIVT-VENTIAPP-CANCELLED-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        cancelled_ventiapp_order.action_confirm()
        cancelled_ventiapp_order.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(cancelled_ventiapp_order.state, 'cancel')

        order_data = {
            'id': 'FIVT-VENTIAPP-CANCELLED-1', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertNotEqual(result, cancelled_ventiapp_order)
        self.assertEqual(result.meli_sync_source, 'xe_meli_connector')
        self.assertFalse(
            cancelled_ventiapp_order.meli_sync_source,
            "the cancelled Ventiapp order must be left completely untouched",
        )
        self.assertEqual(
            self.env['sale.order'].search_count([
                ('reference', '=', 'FIVT-VENTIAPP-CANCELLED-1'),
            ]),
            2,
            "the cancelled order and the newly created order must both exist",
        )

    def test_ambiguous_multiple_ventiapp_siblings_are_not_adopted(self):
        """Fix 3b (2026-09-09, final review — Important): Ventiapp can
        have MULTIPLE sibling sale.orders sharing one reference value
        for a genuine, not-yet-consolidated pack. Adopting an arbitrary
        one of several candidates would corrupt pack-sibling resolution
        for later notifications — so none of them is adopted, a new
        order is created normally, and it carries a manual-review
        chatter message naming the ambiguity (count + reference).
        """
        sibling_a = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-PACK-1',
            'client_order_ref': 'FIVT-VENTIAPP-PACK-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        sibling_b = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-PACK-1',
            'client_order_ref': 'FIVT-VENTIAPP-PACK-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })

        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-PACKAMBIG1',
        })
        order_data = {
            'id': '5000000001', 'status': 'paid', 'pack_id': 'FIVT-VENTIAPP-PACK-1',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-PACKAMBIG1', 'seller_sku': 'ZTEST-PACKAMBIG1'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )

        self.assertNotIn(result, (sibling_a, sibling_b))
        self.assertFalse(sibling_a.meli_sync_source)
        self.assertFalse(sibling_b.meli_sync_source)
        self.assertEqual(result.meli_sync_source, 'xe_meli_connector')
        self.assertTrue(any(
            'FIVT-VENTIAPP-PACK-1' in (msg.body or '') and '2' in (msg.body or '')
            for msg in result.message_ids
        ), "the manual-review message must name both the count and the reference")

    def test_adopted_full_order_cancelled_is_never_auto_processed(self):
        """Fix A (2026-09-09, re-review of the final-review fix round —
        Critical): _meli_flag_status_change's destructive-automation
        gate was keyed only on meli_sync_source, which adoption also
        sets — so an ADOPTED order sitting in the Full fulfillment
        warehouse got auto-returned/cancelled/credit-noted on a normal,
        later 'cancelled' notification, with zero human review,
        contradicting the adoption's own explicit "commercial details
        left untouched" promise (see
        test_ventiapp_order_is_adopted_not_duplicated's chatter
        assertion). Must instead fall through to the exact same
        manual-review message a non-Full order already gets (see
        TestMeliFullCancellationAutomation.
        test_non_full_order_cancelled_only_gets_the_manual_review_message).
        """
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        ventiapp_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-FULLCANCEL-1',
            'client_order_ref': 'FIVT-VENTIAPP-FULLCANCEL-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        ventiapp_order.action_confirm()
        ventiapp_order.picking_ids.button_validate()

        order_data = {
            'id': 'FIVT-VENTIAPP-FULLCANCEL-1', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, order_data,
        )
        self.assertEqual(result, ventiapp_order)
        self.assertTrue(ventiapp_order.meli_adopted)
        self.assertEqual(ventiapp_order.warehouse_id, self.warehouse_fulfillment)

        picking_count_before = len(ventiapp_order.picking_ids)

        ventiapp_order._meli_flag_status_change({'status': 'cancelled'})

        self.assertEqual(
            ventiapp_order.state, 'sale',
            "an adopted order must never be auto-cancelled",
        )
        self.assertEqual(
            len(ventiapp_order.picking_ids), picking_count_before,
            "no stock return must have been created for an adopted order",
        )
        self.assertTrue(any(
            'Review manually' in (msg.body or '') for msg in ventiapp_order.message_ids
        ), "the manual-review message must have been posted instead")

    def test_adopted_pack_order_sibling_line_is_not_added(self):
        """Fix B (2026-09-09, re-review of the final-review fix round —
        Important): a later individual Mercado Libre order for the same
        pack must not get its line silently added to an ADOPTED pack
        sale.order — _meli_add_pack_sibling_lines adds new commercial
        order lines (and can auto-validate a Full delivery), exactly
        what the adoption's own chatter message promises will never
        happen. A manual-review message naming the new sibling order
        must be posted instead, mirroring
        test_ambiguous_multiple_ventiapp_siblings_are_not_adopted's own
        "found something, didn't automate on it" pattern.
        """
        ventiapp_pack_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'reference': 'FIVT-VENTIAPP-PACKADOPT-1',
            'client_order_ref': 'FIVT-VENTIAPP-PACKADOPT-1',
            'origin': 'VENTIAPP-ML-XEBRANDS',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })

        first_order_data = {
            'id': '5100000001', 'status': 'paid',
            'pack_id': 'FIVT-VENTIAPP-PACKADOPT-1',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, first_order_data,
        )
        self.assertEqual(result, ventiapp_pack_order)
        self.assertTrue(ventiapp_pack_order.meli_adopted)
        self.assertEqual(
            ventiapp_pack_order.meli_pack_id, 'FIVT-VENTIAPP-PACKADOPT-1',
        )

        line_count_before = len(ventiapp_pack_order.order_line)

        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-PACKADOPT2',
        })
        second_order_data = {
            'id': '5100000002', 'status': 'paid',
            'pack_id': 'FIVT-VENTIAPP-PACKADOPT-1',
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-PACKADOPT2', 'seller_sku': 'ZTEST-PACKADOPT2'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        second_result = self.env['sale.order']._meli_create_from_order_data(
            self.config, second_order_data,
        )

        self.assertEqual(second_result, ventiapp_pack_order)
        self.assertEqual(
            len(ventiapp_pack_order.order_line), line_count_before,
            "an adopted pack order must never get a new sibling's line added",
        )
        self.assertTrue(any(
            '5100000002' in (msg.body or '') for msg in ventiapp_pack_order.message_ids
        ), "the manual-review message must name the new sibling order")

    def test_action_confirm_failure_does_not_lose_the_sale(self):
        """A base_automation (or any other) failure during
        action_confirm() must leave the sale.order alive, in draft, with
        a manual-review chatter message — never roll back its own
        creation.
        """
        order_data = {
            'id': 'FIVT-CONFIRMFAIL-1', 'status': 'paid', 'pack_id': None,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-CF1', 'seller_sku': self.product.default_code or 'CF1'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        self.env['meli.sku.mapping'].create({
            'product_id': self.product.id, 'meli_sku': 'ZTEST-CF1',
        })
        order_data['order_items'][0]['item']['seller_sku'] = 'ZTEST-CF1'
        with patch.object(
            type(self.env['sale.order']), 'action_confirm',
            side_effect=UserError('simulated automation block'),
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )
        self.assertTrue(order.exists())
        self.assertEqual(order.state, 'draft')
        self.assertTrue(any(
            'simulated automation block' in (msg.body or '')
            for msg in order.message_ids
        ))

    def test_ensure_delivery_regenerates_a_missing_picking(self):
        """Direct unit test of the new _meli_ensure_delivery helper —
        mirrors the real 2026-08-28 incident (some other automation
        already confirmed the order before this connector's own
        action_confirm() check ran, so no picking was ever created via
        this connector's own path): given an order that's already
        'sale' with no live picking, the helper must generate one via
        Odoo's own real stock-rule mechanism (not by skipping and doing
        nothing) — and, for a Full order, validate it too.
        """
        # Needed so the regenerated picking's move actually reserves a
        # quantity: button_validate() (called by
        # _meli_auto_validate_full_pickings, exercised below) refuses to
        # validate a transfer with nothing reserved/encoded — same setup
        # this class's own _create_delivered_order helper already uses
        # for the same reason.
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-DELIVREGEN-1',
            'meli_order_id': 'FIVT-DELIVREGEN-1',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        # Simulates "something else already confirmed this order" —
        # action_confirm() is what this test intentionally does NOT
        # attribute to this connector's own code path; only its
        # end state (order.state == 'sale') matters to the helper.
        order.action_confirm()
        order.picking_ids.action_cancel()
        self.assertFalse(order.picking_ids.filtered(lambda p: p.state != 'cancel'))

        order._meli_ensure_delivery(True)

        pickings = order.picking_ids.filtered(lambda p: p.state != 'cancel')
        self.assertTrue(pickings, "a fresh delivery must have been generated")
        self.assertEqual(pickings.state, 'done', "Full: must also auto-validate")

    def test_ensure_delivery_leaves_non_full_picking_pending(self):
        """Same missing-picking scenario, but non-Full: the delivery
        must still be generated (never silently skipped), but NOT
        auto-validated — matches the explicit 2026-08-28 user decision
        that non-Full transfers need a person to actually pick/pack/ship.
        """
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-DELIVREGEN-2',
            'meli_order_id': 'FIVT-DELIVREGEN-2',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_default.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order.action_confirm()
        order.picking_ids.action_cancel()

        order._meli_ensure_delivery(False)

        pickings = order.picking_ids.filtered(lambda p: p.state != 'cancel')
        self.assertTrue(pickings, "a fresh delivery must have been generated")
        self.assertNotEqual(pickings.state, 'done', "non-Full: must stay pending")

    def test_full_picking_validates_with_zero_tracked_stock(self):
        """The Full fulfillment warehouse has no real Odoo-tracked stock
        (Mercado Libre's own warehouse manages it) — the picking must
        still validate automatically, exactly as the module's own
        docstring promises ("bookkeeping, not something a person needs
        to physically pack"), never get stuck waiting for a reservation
        that can never succeed.
        """
        # Deliberately NOT calling _update_available_quantity — this
        # product genuinely has zero stock anywhere, matching the real
        # ML-warehouse condition under test.
        order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-ZEROSTOCK-1',
            'meli_order_id': 'FIVT-ZEROSTOCK-1',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 3,
            })],
        })
        order.action_confirm()
        picking = order.picking_ids
        self.assertEqual(picking.state, 'confirmed')
        for move in picking.move_ids:
            self.assertEqual(move.quantity, 0.0)

        order._meli_auto_validate_full_pickings()

        self.assertEqual(picking.state, 'done')
        for move in picking.move_ids:
            self.assertEqual(move.quantity, 3.0)

    def test_delivery_recovery_wizard_heals_stuck_and_missing_pickings(self):
        """One wizard run must: (a) validate a Full picking stuck in
        'confirmed' with zero reserved quantity (the ~62-stuck-picking
        production pattern), and (b) generate + validate a missing
        delivery for a Full order that has none at all (the 48-order
        2026-08-28 incident pattern) — scoped to this connector's own
        Full orders only.
        """
        stuck_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-STUCK-1',
            'meli_order_id': 'FIVT-STUCK-1',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        stuck_order.action_confirm()
        self.assertEqual(stuck_order.picking_ids.state, 'confirmed')

        missing_order = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-MISSING-1',
            'meli_order_id': 'FIVT-MISSING-1',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        missing_order.action_confirm()
        missing_order.picking_ids.action_cancel()
        self.assertFalse(
            missing_order.picking_ids.filtered(lambda p: p.state != 'cancel')
        )

        # Fix 4 (2026-09-09, final review — Important): action_run() is
        # now scoped to self.env.company.id — with_company(self.
        # test_company) here matches the exact pattern this suite's own
        # sibling recovery-wizard tests already use (see
        # test_meli_claim_recovery_wizard.py/test_meli_invoice_recovery_
        # wizard.py's own `_wizard()` helpers).
        wizard = self.env['meli.delivery.recovery.wizard'].with_company(
            self.test_company
        ).create({})
        wizard.action_run()

        self.assertEqual(stuck_order.picking_ids.state, 'done')
        new_pickings = missing_order.picking_ids.filtered(lambda p: p.state != 'cancel')
        self.assertTrue(new_pickings)
        self.assertEqual(new_pickings.state, 'done')

    def test_delivery_recovery_wizard_is_scoped_to_the_current_company(self):
        """Fix 4 (2026-09-09, final review — Important): action_run() had
        no company filter at all before this fix — a person running this
        irreversible, inventory-moving wizard in one company's context
        could reach into and heal an unrelated company's own orders.
        Two companies, each with its own Full order needing healing:
        running the wizard scoped to company A must heal ONLY company
        A's order, leaving company B's completely untouched.
        """
        company_b = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli delivery recovery, company B)'})
        warehouse_b = self.env['stock.warehouse'].create({
            'name': 'Almacen Full Recovery B', 'code': 'FRWB',
            'company_id': company_b.id,
        })
        product_b = self.env['product.product'].create({
            'name': 'Producto Recovery B', 'type': 'product',
            'company_id': company_b.id,
        })
        self.env['meli.config'].create({
            'company_id': company_b.id,
            'client_id': 'test-client-id-recovery-b',
            'client_secret': 'test-client-secret-recovery-b',
            'state': 'connected',
            'partner_id': self.partner.id,
            'warehouse_fulfillment_id': warehouse_b.id,
        })

        order_a = self.env['sale.order'].create({
            'company_id': self.test_company.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-SCOPE-A',
            'meli_order_id': 'FIVT-SCOPE-A',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': self.warehouse_fulfillment.id,
            'order_line': [(0, 0, {
                'product_id': self.product.id, 'product_uom_qty': 1,
            })],
        })
        order_a.action_confirm()
        order_a.picking_ids.action_cancel()
        self.assertFalse(order_a.picking_ids.filtered(lambda p: p.state != 'cancel'))

        order_b = self.env['sale.order'].create({
            'company_id': company_b.id,
            'partner_id': self.partner.id,
            'client_order_ref': 'FIVT-SCOPE-B',
            'meli_order_id': 'FIVT-SCOPE-B',
            'meli_sync_source': 'xe_meli_connector',
            'warehouse_id': warehouse_b.id,
            'order_line': [(0, 0, {
                'product_id': product_b.id, 'product_uom_qty': 1,
            })],
        })
        order_b.action_confirm()
        order_b.picking_ids.action_cancel()
        self.assertFalse(order_b.picking_ids.filtered(lambda p: p.state != 'cancel'))

        wizard = self.env['meli.delivery.recovery.wizard'].with_company(
            self.test_company
        ).create({})
        wizard.action_run()

        self.assertTrue(
            order_a.picking_ids.filtered(lambda p: p.state != 'cancel'),
            "company A's order must be healed",
        )
        self.assertFalse(
            order_b.picking_ids.filtered(lambda p: p.state != 'cancel'),
            "company B's order must be left completely untouched",
        )
