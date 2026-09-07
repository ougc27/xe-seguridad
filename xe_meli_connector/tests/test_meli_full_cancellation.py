from unittest.mock import patch

from dateutil.relativedelta import relativedelta
import pytz

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.xe_meli_connector.models.sale_order import MELI_FISCAL_TIMEZONE


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

    def _mx_local_now(self):
        """Mexico-local wall-clock 'now', naive — matches what
        l10n_mx_edi_post_time actually holds in production (see the
        docstring of _meli_invoice_stamped_same_month_as_today). Building
        "stamped this/last month" fixtures from raw fields.Datetime.now()
        (real UTC) instead would make them silently depend on what time
        of day the suite happens to run: for roughly six hours out of
        every day (00:00-06:00 UTC), Monterrey is still on the previous
        UTC day, so a fixture meant to be "this month" or "last month"
        could land on the wrong side of a month boundary purely by
        coincidence of the wall clock — exactly the kind of ambiguity
        re-review finding #1 is about.
        """
        return pytz.utc.localize(fields.Datetime.now()).astimezone(
            MELI_FISCAL_TIMEZONE
        ).replace(tzinfo=None)

    def _post_invoice_with_fake_cfdi(self, order, post_time, invoice=None):
        """Posts an invoice of `order` and gives it a stamped CFDI
        document, without ever going near a real PAC.
        """
        invoice = invoice if invoice is not None else order._create_invoices()
        invoice.action_post()
        invoice.l10n_mx_edi_post_time = post_time
        self.env['l10n_mx_edi.document'].create({
            'invoice_ids': [(4, invoice.id)],
            'datetime': post_time,
            'state': 'invoice_sent',
        })
        return invoice

    def _live_cfdi_document(self, invoice):
        return invoice.l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_sent'
        )[:1]

    def _cancel_outcome(self, invoice, state, sat_state=None, message=None):
        """Side effect for the mocked
        _l10n_mx_edi_cfdi_invoice_try_cancel.

        The real method NEVER raises on a PAC/SAT rejection: it records
        the outcome on an l10n_mx_edi.document and returns normally
        (enterprise/l10n_mx_edi/models/account_move.py, on_failure /
        on_success). A bare mock therefore makes every cancellation look
        like a success — which is exactly the bug Critical #2 is about.

        It also leaves the original 'invoice_sent' document in place and
        records the attempt on a NEWER one (see
        _create_update_invoice_document_from_invoice), so the code under
        test has to read the newest document rather than the one it
        handed in. Reproduced faithfully here.
        """
        def _side_effect(*args, **kwargs):
            self.env['l10n_mx_edi.document'].create({
                'invoice_ids': [(4, invoice.id)],
                'datetime': fields.Datetime.now() + relativedelta(seconds=30),
                'state': state,
                'sat_state': sat_state,
                'message': message,
            })
        return _side_effect

    def _fake_stamp(self, *args, **kwargs):
        """Side effect for the mocked
        _l10n_mx_edi_cfdi_invoice_try_send: a PAC can't be called from a
        test, so the successful outcome is arranged here — a stamped
        document, which is what makes l10n_mx_edi_cfdi_state compute to
        'sent'. Used with autospec=True, so the record comes in as the
        first argument.
        """
        move = args[0]
        self.env['l10n_mx_edi.document'].create({
            'invoice_ids': [(4, move.id)],
            'datetime': fields.Datetime.now(),
            'state': 'invoice_sent',
        })

    def test_stamped_same_month_handles_early_morning_mexico_local_stamp(self):
        """Re-review finding #1: l10n_mx_edi_post_time is already
        Mexico-local wall-clock time (not UTC, despite looking like a
        plain Datetime) — see the docstring of
        _meli_invoice_stamped_same_month_as_today for the full citation
        trail. The old code re-localized it as UTC and shifted it back
        another six hours, which pushed an early-morning stamp on the 1st
        of the month into the *previous* month and would have wrongly
        issued a credit note instead of cancelling the CFDI.
        """
        order = self._create_full_order('FCXT-0026')
        day1_early_morning_local = fields.Datetime.now().replace(
            day=1, hour=2, minute=0, second=0, microsecond=0,
        )
        invoice = self._post_invoice_with_fake_cfdi(
            order, day1_early_morning_local,
        )
        # "Now" (real UTC) resolves to later in that same calendar month,
        # once converted to Monterrey/CDMX — unambiguously the same month
        # as the stamp above, on both the buggy and the fixed reading of
        # the stamp... except the bug's extra -6h shift on the STAMP side
        # is what pushes it out of this month, not this side.
        later_same_month_utc = day1_early_morning_local.replace(
            day=15, hour=18, minute=0, second=0, microsecond=0,
        )

        with patch.object(
            fields.Datetime, 'now', return_value=later_same_month_utc,
        ):
            result = order._meli_invoice_stamped_same_month_as_today(invoice)

        self.assertTrue(
            result,
            "an invoice stamped at 02:00 Monterrey time on the 1st of the "
            "month was misjudged as belonging to the previous month",
        )

    def test_full_order_cancelled_same_month_cancels_the_cfdi(self):
        order = self._create_full_order('FCXT-0007')
        invoice = self._post_invoice_with_fake_cfdi(
            order, self._mx_local_now(),
        )
        document = self._live_cfdi_document(invoice)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
            side_effect=self._cancel_outcome(
                invoice, 'invoice_cancel_requested', sat_state='not_defined',
            ),
        ) as mock_cancel, patch.object(
            type(self.env['l10n_mx_edi.document']), '_fetch_and_update_sat_status',
        ) as mock_fetch:
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        mock_cancel.assert_called_once()
        self.assertEqual(mock_cancel.call_args.args[-1], '02')
        mock_fetch.assert_called_once()
        # Regression guard: the SAT-status fetch must stay scoped to just
        # this one document — a future change that widens this domain
        # would otherwise re-check every other document in the whole
        # database on every single cancellation.
        self.assertEqual(
            mock_fetch.call_args.kwargs.get('extra_domain'),
            [('id', '=', document.id)],
        )
        body = self._chatter(order)
        self.assertIn('cfdi cancellation was requested', body)
        self.assertNotIn('rejected', body)
        self.assertNotIn('did not fully succeed', body)

    def test_same_month_cfdi_cancellation_rejected_is_reported_as_a_failure(self):
        """Critical #2: _l10n_mx_edi_cfdi_invoice_try_cancel returns
        normally when the PAC rejects the cancellation, so the outcome
        has to be re-read from the newest l10n_mx_edi.document. Before
        this fix the chatter claimed success for a CFDI that is still
        live at the SAT.
        """
        order = self._create_full_order('FCXT-0023')
        invoice = self._post_invoice_with_fake_cfdi(
            order, self._mx_local_now(),
        )

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
            side_effect=self._cancel_outcome(
                invoice, 'invoice_cancel_requested_failed',
                message='PAC rejected: CFDI already has a related document',
            ),
        ), patch.object(
            type(self.env['l10n_mx_edi.document']), '_fetch_and_update_sat_status',
        ):
            order._meli_flag_status_change(self._order_data())

        # Phase 1 still went through — that's the whole point of the
        # two-phase design.
        self.assertEqual(order.state, 'cancel')
        body = self._chatter(order)
        self.assertIn('was rejected', body)
        self.assertIn('pac rejected: cfdi already has a related document', body)
        self.assertNotIn('cfdi cancellation was requested', body)
        self.assertIn('did not fully succeed', body)

    def test_same_month_invoice_without_active_cfdi_document_is_reported(self):
        """Important #5: a posted invoice with no live CFDI document used
        to be skipped in silence — no action text, no failure signal.
        """
        order = self._create_full_order('FCXT-0024')
        invoice = order._create_invoices()
        invoice.action_post()  # posted this month, but never stamped
        self.assertFalse(invoice.l10n_mx_edi_invoice_document_ids)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
        ) as mock_cancel:
            order._meli_flag_status_change(self._order_data())

        mock_cancel.assert_not_called()
        self.assertEqual(order.state, 'cancel')
        body = self._chatter(order)
        self.assertIn('no active cfdi document to cancel', body)
        self.assertIn('did not fully succeed', body)

    def _chatter(self, order):
        return '\n'.join(msg.body or '' for msg in order.message_ids).lower()

    def test_no_posted_invoice_is_only_returned_and_cancelled(self):
        order = self._create_full_order('FCXT-0008')
        order._create_invoices()  # left in draft, never posted

        order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')

    def test_invoice_from_previous_month_issues_credit_note_not_cfdi_cancel(self):
        # A previous-month invoice goes through the credit-note branch of
        # _meli_resolve_full_invoice, not the same-month/CFDI-cancel branch
        # (see test_full_order_cancelled_previous_month_issues_a_credit_note
        # for the credit note's own assertions — type, state, chatter
        # message). This test's own assertion is that the CFDI-cancel path
        # is never taken here, and that the original invoice itself is
        # left posted (a credit note is issued against it, but the invoice
        # is never cancelled/modified in place).
        order = self._create_full_order('FCXT-0009')
        last_month = self._mx_local_now() - relativedelta(months=1)
        invoice = self._post_invoice_with_fake_cfdi(order, last_month)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
        ) as mock_cancel, patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_send',
            autospec=True, side_effect=self._fake_stamp,
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        mock_cancel.assert_not_called()
        self.assertEqual(invoice.state, 'posted')

    def test_full_order_cancelled_previous_month_issues_a_credit_note(self):
        """Critical #1: the credit note must be posted AND stamped.
        action_post() alone only records l10n_mx_edi_post_time — the PAC
        call is a separate step, so before this fix the automation left a
        credit note that existed in Odoo and nowhere at the SAT.
        """
        order = self._create_full_order('FCXT-0011')
        last_month = self._mx_local_now() - relativedelta(months=1)
        invoice = self._post_invoice_with_fake_cfdi(order, last_month)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
        ) as mock_cancel, patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_send',
            autospec=True, side_effect=self._fake_stamp,
        ) as mock_send:
            order._meli_flag_status_change(self._order_data())

        mock_cancel.assert_not_called()
        credit_note = self.env['account.move'].search([
            ('reversed_entry_id', '=', invoice.id),
        ])
        self.assertEqual(len(credit_note), 1)
        self.assertEqual(credit_note.move_type, 'out_refund')
        self.assertEqual(credit_note.state, 'posted')
        # The stamping call is made, and it is made on the credit note
        # itself — not on the original invoice.
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args.args[0], credit_note)
        self.assertEqual(credit_note.l10n_mx_edi_cfdi_state, 'sent')
        body = self._chatter(order)
        self.assertIn('was issued and stamped', body)
        self.assertNotIn('did not fully succeed', body)

    def test_credit_note_that_cannot_be_stamped_is_reported_as_a_failure(self):
        """Critical #1, failure side: _l10n_mx_edi_cfdi_invoice_try_send
        doesn't raise when the PAC rejects the document either, so the
        real CFDI state has to be checked afterwards.
        """
        order = self._create_full_order('FCXT-0012')
        last_month = self._mx_local_now() - relativedelta(months=1)
        invoice = self._post_invoice_with_fake_cfdi(order, last_month)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
        ), patch.object(
            # No side effect: nothing gets stamped, exactly like a PAC
            # rejection, which leaves l10n_mx_edi_cfdi_state unset.
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_send',
            autospec=True,
        ) as mock_send:
            order._meli_flag_status_change(self._order_data())

        mock_send.assert_called_once()
        credit_note = self.env['account.move'].search([
            ('reversed_entry_id', '=', invoice.id),
        ])
        self.assertEqual(len(credit_note), 1)
        self.assertFalse(credit_note.l10n_mx_edi_cfdi_state)
        # Phase 1 still stands.
        self.assertEqual(order.state, 'cancel')
        body = self._chatter(order)
        self.assertIn('could not be stamped', body)
        self.assertNotIn('was issued and stamped', body)
        self.assertIn('did not fully succeed', body)

    def test_existing_credit_note_on_the_order_is_never_reversed_again(self):
        """Important #2: self.invoice_ids also carries any credit note
        already sitting on the order (partial refunds are still handled
        manually today). Reversing one of those would issue a brand-new
        invoice to the customer.
        """
        order = self._create_full_order('FCXT-0013')
        last_month = self._mx_local_now() - relativedelta(months=1)
        invoice = self._post_invoice_with_fake_cfdi(order, last_month)
        # A manual partial refund, the way the team still does them today.
        manual_wizard = self.env['account.move.reversal'].create({
            'move_ids': [(6, 0, invoice.ids)],
            'journal_id': invoice.journal_id.id,
            'company_id': invoice.company_id.id,
            'reason': 'Manual partial refund',
        })
        manual_wizard.reverse_moves(is_modify=False)
        existing_credit_note = manual_wizard.new_move_ids
        existing_credit_note.action_post()
        # sale.order.invoice_ids is computed from order_line.invoice_lines,
        # and account.move.line.sale_line_ids is copy=False, so the
        # reversal doesn't carry the link over by itself. Link it here so
        # the credit note really is part of order.invoice_ids — which is
        # the whole premise of this finding.
        existing_credit_note.invoice_line_ids.write({
            'sale_line_ids': [(6, 0, order.order_line.ids)],
        })
        order.invalidate_recordset(['invoice_ids'])
        self.assertIn(existing_credit_note, order.invoice_ids)

        with patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
        ), patch.object(
            type(invoice), '_l10n_mx_edi_cfdi_invoice_try_send',
            autospec=True, side_effect=self._fake_stamp,
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertFalse(
            self.env['account.move'].search([
                ('reversed_entry_id', '=', existing_credit_note.id),
            ]),
            "the pre-existing credit note was reversed, which would "
            "invoice the customer all over again",
        )
        # The real invoice was still handled.
        self.assertEqual(len(self.env['account.move'].search([
            ('reversed_entry_id', '=', invoice.id),
        ])), 2)  # the manual one + the automatic one

    def test_one_invoice_succeeds_and_a_later_one_fails_both_are_reported(self):
        """Important #6: partial progress used to be lost — the whole
        actions list was thrown away as soon as one invoice failed, even
        though a CFDI cancellation is irreversible and already committed
        by l10n_mx_edi. Both outcomes must reach the chatter.
        """
        order = self._create_full_order('FCXT-0014')
        # Invoice 1: previous month -> credit note, whose stamping fails.
        last_month = self._mx_local_now() - relativedelta(months=1)
        old_invoice = self._post_invoice_with_fake_cfdi(order, last_month)
        # Invoice 2: a second invoice on the same order, stamped this
        # month -> CFDI cancellation, which succeeds. Built directly
        # rather than through _create_invoices(): this product is
        # invoiced on delivered quantities and the order is already fully
        # delivered AND fully invoiced, so the sale flow has nothing left
        # to bill. sale.order.invoice_ids is computed from
        # order_line.invoice_lines, so linking sale_line_ids is what puts
        # this invoice on the order.
        new_invoice = self.env['account.move'].with_company(
            self.test_company
        ).create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'journal_id': old_invoice.journal_id.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'quantity': 1,
                'price_unit': 100.0,
                'sale_line_ids': [(6, 0, order.order_line.ids)],
            })],
        })
        self._post_invoice_with_fake_cfdi(
            order, self._mx_local_now(), invoice=new_invoice,
        )
        order.invalidate_recordset(['invoice_ids'])
        self.assertEqual(len(order.invoice_ids), 2)

        with patch.object(
            type(old_invoice), '_l10n_mx_edi_cfdi_invoice_try_cancel',
            side_effect=self._cancel_outcome(
                new_invoice, 'invoice_cancel_requested',
                sat_state='not_defined',
            ),
        ), patch.object(
            type(self.env['l10n_mx_edi.document']), '_fetch_and_update_sat_status',
        ), patch.object(
            # PAC rejection for the credit note.
            type(old_invoice), '_l10n_mx_edi_cfdi_invoice_try_send',
            autospec=True,
        ):
            order._meli_flag_status_change(self._order_data())

        body = self._chatter(order)
        # The successful CFDI cancellation is reported...
        self.assertIn('cfdi cancellation was requested', body)
        # ...alongside the failed credit-note stamping, in the same message.
        self.assertIn('could not be stamped', body)
        self.assertIn('did not fully succeed', body)

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
        self._post_invoice_with_fake_cfdi(order, fields.Datetime.now())
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
            type(order), '_meli_invoice_stamped_same_month_as_today',
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

    def test_invoice_resolution_failure_does_not_roll_back_stock_and_sale(self):
        # The load-bearing case for the two-phase design: Odoo's own
        # l10n_mx_edi PAC-calling code does a raw cr.commit() on every
        # exit path in production, which would silently invalidate a
        # savepoint shared with invoice resolution. Phase 1 (stock
        # return + sale cancellation) is committed BEFORE Phase 2
        # (invoice resolution) is attempted, so a Phase 2 failure must
        # never undo Phase 1's already-committed work.
        order = self._create_full_order('FCXT-0010')
        picking_count_before = len(order.picking_ids)

        with patch.object(
            type(order), '_meli_resolve_full_invoice',
            side_effect=Exception('boom'),
        ):
            order._meli_flag_status_change(self._order_data())

        self.assertEqual(order.state, 'cancel')
        self.assertEqual(len(order.picking_ids), picking_count_before + 1)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))
        messages = '<br/>'.join(msg.body or '' for msg in order.message_ids)
        self.assertIn('cancelled', messages.lower())
        self.assertIn('manual review', messages.lower())
