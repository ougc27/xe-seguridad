import base64
import io
from unittest.mock import patch

import openpyxl

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..wizards.meli_import_batch_wizard import _meli_extract_order_ids


def _make_xlsx(rows):
    """Builds an in-memory .xlsx with one row per item in `rows` (each
    item becomes the value of column A), returned as raw bytes — mirrors
    what the wizard receives via its Binary field.
    """
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append([row])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@tagged('post_install', '-at_install')
class TestMeliExtractOrderIds(TransactionCase):
    """Pure parsing logic — no Odoo records involved."""

    def test_valid_numeric_text_and_float_cells_are_recognized(self):
        content = _make_xlsx([
            'order_id',  # header row, not a valid order id
            '2000018165886068',  # text
            2000018165886069,  # int
            2000018165886070.0,  # float, whole number
        ])
        rows = _meli_extract_order_ids(content)
        self.assertEqual(rows, [
            ('order_id', None),
            ('2000018165886068', '2000018165886068'),
            ('2000018165886069', '2000018165886069'),
            ('2000018165886070', '2000018165886070'),
        ])

    def test_blank_rows_are_skipped_entirely(self):
        content = _make_xlsx(['2000018165886068', None, '2000018165886069'])
        rows = _meli_extract_order_ids(content)
        self.assertEqual(len(rows), 2)

    def test_non_numeric_text_is_reported_as_invalid(self):
        content = _make_xlsx(['not-an-order-id'])
        rows = _meli_extract_order_ids(content)
        self.assertEqual(rows, [('not-an-order-id', None)])


@tagged('post_install', '-at_install')
class TestMeliImportBatchWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli import batch)'})

        cls.product = cls.env['product.product'].create({
            'name': 'Producto de prueba', 'company_id': cls.test_company.id,
        })
        cls.env['meli.sku.mapping'].create({
            'product_id': cls.product.id, 'meli_sku': 'ZTEST-BATCH01',
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Test', 'x_cop': 'cliente',
        })
        cls.salesperson = cls.env['res.users'].create({
            'name': 'Meli Batch Test User', 'login': 'meli_batch_test_user',
        })
        cls.team = cls.env['crm.team'].create({
            'name': 'MARKETPLACE Batch Test', 'company_id': cls.test_company.id,
        })
        cls.warehouse_fulfillment = cls.env['stock.warehouse'].create({
            'name': 'Almacén Full Batch Test', 'code': 'FULB',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacén Default Batch Test', 'code': 'DEFB',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'state': 'connected',
            'access_token': 'test-access-token',
            'refresh_token': 'test-refresh-token',
            'partner_id': cls.partner.id,
            'sale_team_id': cls.team.id,
            'salesperson_id': cls.salesperson.id,
            'warehouse_fulfillment_id': cls.warehouse_fulfillment.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _order_data(self, order_id, status='paid'):
        return {
            'id': order_id, 'status': status, 'pack_id': None, 'shipping': {},
            'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM123', 'seller_sku': 'ZTEST-BATCH01'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }

    def _wizard(self, xlsx_rows):
        content = _make_xlsx(xlsx_rows)
        return self.env['meli.import.batch.wizard'].with_company(
            self.test_company
        ).create({
            'excel_file': base64.b64encode(content),
            'filename': 'orders.xlsx',
        })

    def test_import_without_active_connection_raises(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli connection)'})
        wizard = self.env['meli.import.batch.wizard'].with_company(
            other_company
        ).create({
            'excel_file': base64.b64encode(_make_xlsx(['123'])),
            'filename': 'orders.xlsx',
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_invalid_and_duplicate_rows_are_reported_without_jobs(self):
        wizard = self._wizard(['order_id', '3000000000000001', '3000000000000001'])
        action = wizard.action_import()

        batch = self.env['meli.import.batch'].browse(action['res_id'])
        statuses = {line.raw_value: line.status for line in batch.line_ids}
        self.assertEqual(statuses['order_id'], 'invalid')
        lines_for_dup = batch.line_ids.filtered(lambda l: l.raw_value == '3000000000000001')
        # The queue_job for the first occurrence is only enqueued here, not
        # run synchronously (no jobrunner in this test) — it stays 'pending'
        # until a worker picks it up, exactly like every other new line.
        self.assertEqual(sorted(lines_for_dup.mapped('status')), ['duplicate', 'pending'])

    def test_already_existing_order_is_flagged_without_a_new_job(self):
        existing_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'client_order_ref': '4000000000000001',
            'meli_order_id': '4000000000000001',
            'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
        })
        JobModel = self.env['queue.job'].sudo()
        before = JobModel.search_count([
            ('identity_key', '=', 'meli_import_order_4000000000000001'),
        ])
        wizard = self._wizard(['4000000000000001'])
        action = wizard.action_import()
        batch = self.env['meli.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'already_existed')
        self.assertEqual(batch.line_ids.sale_order_id, existing_order)
        after = JobModel.search_count([
            ('identity_key', '=', 'meli_import_order_4000000000000001'),
        ])
        self.assertEqual(before, after)

    def test_new_order_is_queued_and_line_starts_pending(self):
        wizard = self._wizard(['5000000000000001'])
        action = wizard.action_import()
        batch = self.env['meli.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'pending')
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_order_5000000000000001'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'sale.order')
        self.assertEqual(job.method_name, '_meli_import_order_for_batch_line')


@tagged('post_install', '-at_install')
class TestMeliImportBatchWizardPacing(TransactionCase):
    """Deliberately its own, self-contained fixture — NOT reusing
    TestMeliImportBatchWizard's own setUpClass, which sets
    res.partner.x_cop (an xe_pacific field, not installed in this
    shared test database) and errors out unconditionally, so nothing
    added there would ever actually execute (confirmed fact from
    earlier work today, not a guess).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli import batch pacing)'})
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Pacing Test',
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacen Default Pacing Test', 'code': 'DEFP',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id-pacing',
            'client_secret': 'test-client-secret-pacing',
            'state': 'connected',
            'partner_id': cls.partner.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _wizard(self, xlsx_rows):
        content = _make_xlsx(xlsx_rows)
        return self.env['meli.import.batch.wizard'].with_company(
            self.test_company
        ).create({
            'excel_file': base64.b64encode(content),
            'filename': 'orders.xlsx',
        })

    def test_jobs_are_staggered_three_seconds_apart(self):
        """The bug this closes: an Excel with many rows used to enqueue
        every job with the same eta (right now), which — combined with
        the since-fixed queue_job_cron_jobrunner retry_pattern bug —
        self-inflicted a burst of API traffic against Mercado Libre
        (production, 2026-09-10). Each successfully-enqueued row must
        now be 3 seconds further out than the previous one.
        """
        wizard = self._wizard([
            '6000000000000001', '6000000000000002',
            '6000000000000003', '6000000000000004',
        ])
        wizard.action_import()

        jobs = self.env['queue.job'].sudo().search([
            ('identity_key', 'in', [
                'meli_import_order_6000000000000001',
                'meli_import_order_6000000000000002',
                'meli_import_order_6000000000000003',
                'meli_import_order_6000000000000004',
            ]),
        ], order='id asc')
        self.assertEqual(len(jobs), 4)
        # The very first enqueued row gets eta=0 seconds — with_delay()
        # treats that as "no delay at all" (falsy), same as never
        # passing eta, so it runs as soon as a worker is free, not at a
        # literal "now" timestamp. Only rows 2+ get a real, non-zero eta.
        self.assertFalse(jobs[0].eta, "the first row runs immediately, no eta")
        etas = jobs[1:].mapped('eta')
        self.assertTrue(all(etas), "every OTHER enqueued job must have a real eta")
        gaps = [
            (etas[i + 1] - etas[i]).total_seconds()
            for i in range(len(etas) - 1)
        ]
        # Each with_delay() call reads "now" at a slightly later wall-clock
        # moment than the previous loop iteration (real, if tiny, Python
        # overhead between them) — so the gap is "3 seconds plus a few
        # milliseconds," never exactly 3.0. A generous 1-second tolerance
        # only fails if the real pacing logic breaks, not on test-machine
        # timing noise.
        for gap in gaps:
            self.assertAlmostEqual(gap, 3.0, delta=1.0)

    def test_skipped_rows_do_not_waste_the_pacing_budget(self):
        """A duplicate/invalid row never calls with_delay() at all — it
        must not still consume a "slot" in the stagger, or a file with
        many skipped rows would push every real job needlessly further
        out. Uses 3 real rows (not 2) so the comparison lands on the
        2nd and 3rd real enqueues, both of which get a genuine non-zero
        eta — the 1st real row's own eta=0 is falsy (see the sibling
        test above), so it can't be used in a subtraction here.
        """
        wizard = self._wizard([
            '6000000000000010', 'not-a-number', '6000000000000010',
            '6000000000000011', '6000000000000012',
        ])
        wizard.action_import()

        jobs = self.env['queue.job'].sudo().search([
            ('identity_key', 'in', [
                'meli_import_order_6000000000000010',
                'meli_import_order_6000000000000011',
                'meli_import_order_6000000000000012',
            ]),
        ], order='id asc')
        self.assertEqual(len(jobs), 3)
        self.assertFalse(jobs[0].eta)
        self.assertTrue(jobs[1].eta)
        self.assertTrue(jobs[2].eta)
        self.assertAlmostEqual(
            (jobs[2].eta - jobs[1].eta).total_seconds(), 3.0, delta=1.0,
            msg="only the 3 real, enqueued rows count toward the stagger — "
                "the invalid row and the duplicate must not shift this gap",
        )


@tagged('post_install', '-at_install')
class TestMeliImportOrderForBatchLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli import batch line)'})
        cls.product = cls.env['product.product'].create({
            'name': 'Producto de prueba 2', 'company_id': cls.test_company.id,
        })
        cls.env['meli.sku.mapping'].create({
            'product_id': cls.product.id, 'meli_sku': 'ZTEST-BATCH02',
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Test 2', 'x_cop': 'cliente',
        })
        cls.salesperson = cls.env['res.users'].create({
            'name': 'Meli Batch Line Test User', 'login': 'meli_batch_line_test_user',
        })
        cls.team = cls.env['crm.team'].create({
            'name': 'MARKETPLACE Batch Line Test', 'company_id': cls.test_company.id,
        })
        cls.warehouse_fulfillment = cls.env['stock.warehouse'].create({
            'name': 'Almacén Full Batch Line Test', 'code': 'FULC',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacén Default Batch Line Test', 'code': 'DEFC',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'state': 'connected',
            'access_token': 'test-access-token',
            'refresh_token': 'test-refresh-token',
            'partner_id': cls.partner.id,
            'sale_team_id': cls.team.id,
            'salesperson_id': cls.salesperson.id,
            'warehouse_fulfillment_id': cls.warehouse_fulfillment.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _order_data(self, order_id, status='paid'):
        return {
            'id': order_id, 'status': status, 'pack_id': None, 'shipping': {},
            'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM456', 'seller_sku': 'ZTEST-BATCH02'},
                'quantity': 1, 'unit_price': 75.0,
            }],
        }

    def _line(self, order_id):
        batch = self.env['meli.import.batch'].create({'company_id': self.test_company.id})
        return self.env['meli.import.batch.line'].create({
            'batch_id': batch.id, 'order_id': order_id, 'status': 'pending',
        })

    def test_success_marks_line_imported_with_sale_order(self):
        line = self._line('6000000000000001')
        with patch.object(type(self.config), '_api_get', return_value=self._order_data('6000000000000001')):
            self.env['sale.order']._meli_import_order_for_batch_line(
                self.test_company.id, '6000000000000001', line.id,
            )
        self.assertEqual(line.status, 'imported')
        self.assertEqual(line.sale_order_id.client_order_ref, '6000000000000001')

    def test_not_paid_order_marks_line_not_paid(self):
        line = self._line('6000000000000002')
        with patch.object(type(self.config), '_api_get', return_value=self._order_data('6000000000000002', status='confirmed')):
            self.env['sale.order']._meli_import_order_for_batch_line(
                self.test_company.id, '6000000000000002', line.id,
            )
        self.assertEqual(line.status, 'not_paid')
        self.assertFalse(line.sale_order_id)

    def test_exception_marks_line_error_without_raising(self):
        line = self._line('6000000000000003')
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'No Meli Config Co'})
        self.env['sale.order']._meli_import_order_for_batch_line(
            other_company.id, '6000000000000003', line.id,
        )
        self.assertEqual(line.status, 'error')
        self.assertTrue(line.message)

    def test_retry_pending_reenqueues_only_non_final_lines(self):
        batch = self.env['meli.import.batch'].create({'company_id': self.test_company.id})
        pending = self.env['meli.import.batch.line'].create({
            'batch_id': batch.id, 'order_id': '6000000000000004', 'status': 'error',
            'message': 'boom',
        })
        done = self.env['meli.import.batch.line'].create({
            'batch_id': batch.id, 'order_id': '6000000000000005', 'status': 'imported',
        })
        JobModel = self.env['queue.job'].sudo()
        before = JobModel.search_count([
            ('identity_key', '=', 'meli_import_order_6000000000000004'),
        ])
        batch.action_retry_pending()
        self.assertEqual(pending.status, 'pending')
        self.assertEqual(done.status, 'imported')
        after = JobModel.search_count([
            ('identity_key', '=', 'meli_import_order_6000000000000004'),
        ])
        self.assertEqual(after, before + 1)
