import base64
import io
from unittest.mock import patch

import openpyxl

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..wizards.meli_invoice_import_batch_wizard import _meli_extract_invoice_batch_rows


def _make_xlsx(rows):
    """Builds an in-memory .xlsx with one row per item in `rows`, returned
    as raw bytes — mirrors what the wizard receives via its Binary field.
    Each item can be a scalar (goes into column A only, columns B/C left
    blank — the shape every pre-existing Invoice-ID-only test here uses)
    or a tuple/list of up to 3 values (Invoice ID, Order ID, Pack ID).
    """
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        if not isinstance(row, (list, tuple)):
            row = (row,)
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@tagged('post_install', '-at_install')
class TestMeliExtractInvoiceBatchRows(TransactionCase):
    """Pure parsing logic — no Odoo records involved."""

    def test_valid_numeric_text_and_float_cells_are_recognized(self):
        content = _make_xlsx([
            'invoice_id',  # header row, not a valid invoice id
            '3000000083516040',  # text
            3000000083516041,  # int
            3000000083516042.0,  # float, whole number
        ])
        rows = _meli_extract_invoice_batch_rows(content)
        self.assertEqual(rows, [
            ('invoice_id', '', '', None, None, None),
            ('3000000083516040', '', '', '3000000083516040', None, None),
            ('3000000083516041', '', '', '3000000083516041', None, None),
            ('3000000083516042', '', '', '3000000083516042', None, None),
        ])

    def test_all_three_columns_are_parsed(self):
        content = _make_xlsx([('7000000000000001', '7000000000000002', '2000000000000003')])
        rows = _meli_extract_invoice_batch_rows(content)
        self.assertEqual(rows, [(
            '7000000000000001', '7000000000000002', '2000000000000003',
            '7000000000000001', '7000000000000002', '2000000000000003',
        )])

    def test_blank_rows_are_skipped_entirely(self):
        content = _make_xlsx(['3000000083516040', None, '3000000083516041'])
        rows = _meli_extract_invoice_batch_rows(content)
        self.assertEqual(len(rows), 2)

    def test_non_numeric_text_is_reported_as_invalid(self):
        content = _make_xlsx(['not-an-invoice-id'])
        rows = _meli_extract_invoice_batch_rows(content)
        self.assertEqual(rows, [('not-an-invoice-id', '', '', None, None, None)])

    def test_row_with_only_order_and_pack_columns(self):
        content = _make_xlsx([(None, '7000000000000005', '2000000000000006')])
        rows = _meli_extract_invoice_batch_rows(content)
        self.assertEqual(rows, [(
            '', '7000000000000005', '2000000000000006',
            None, '7000000000000005', '2000000000000006',
        )])


@tagged('post_install', '-at_install')
class TestMeliInvoiceImportBatchWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice import batch)'})
        cls.partner = cls.env['res.partner'].create({'name': 'Meli Invoice Batch Test Buyer'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'invoice-batch-client', 'client_secret': 'invoice-batch-secret',
            'state': 'connected', 'ml_user_id': '999',
            'partner_id': cls.partner.id,
        })
        cls.warehouse = cls.env['stock.warehouse'].create({
            'name': 'Almacén Invoice Batch Test', 'code': 'IBWT',
            'company_id': cls.test_company.id,
        })

    def _wizard(self, xlsx_rows):
        content = _make_xlsx(xlsx_rows)
        return self.env['meli.invoice.import.batch.wizard'].with_company(
            self.test_company
        ).create({
            'excel_file': base64.b64encode(content),
            'filename': 'invoices.xlsx',
        })

    def _adopted_order(self, pack_id):
        return self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'reference': pack_id, 'client_order_ref': pack_id, 'meli_pack_id': pack_id,
        })

    def test_import_without_active_connection_raises(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli connection, invoices)'})
        wizard = self.env['meli.invoice.import.batch.wizard'].with_company(
            other_company
        ).create({
            'excel_file': base64.b64encode(_make_xlsx(['123'])),
            'filename': 'invoices.xlsx',
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_invalid_and_duplicate_rows_are_reported_without_jobs(self):
        wizard = self._wizard(['invoice_id', '8000000000000001', '8000000000000001'])
        action = wizard.action_import()

        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])
        statuses = {line.raw_value: line.status for line in batch.line_ids}
        self.assertEqual(statuses['invoice_id'], 'invalid')
        lines_for_dup = batch.line_ids.filtered(lambda l: l.raw_value == '8000000000000001')
        # The queue_job for the first occurrence is only enqueued here, not
        # run synchronously (no jobrunner in this test) — it stays 'pending'
        # until a worker picks it up, exactly like every other new line.
        self.assertEqual(sorted(lines_for_dup.mapped('status')), ['duplicate', 'pending'])

    def test_same_invoice_id_covering_different_orders_is_not_a_duplicate(self):
        """Real production file (2026-09-14, 12,809 rows): a single
        Invoice ID legitimately repeats across several DIFFERENT
        orders/packs — one factura can cover more than one order. The
        old key (Invoice ID alone) silently dropped every row after the
        first as 'duplicate', losing those extra orders entirely. Only
        an EXACT repeat of (invoice, order, pack) is a real duplicate.
        """
        wizard = self._wizard([
            ('8000000000000030', '2000000000000030', '2000000000000030'),
            ('8000000000000030', '2000000000000031', '2000000000000031'),
        ])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        lines = batch.line_ids.sorted('id')
        self.assertEqual(lines.mapped('status'), ['pending', 'pending'])
        self.assertEqual(lines.mapped('order_id'), ['2000000000000030', '2000000000000031'])

    def test_exact_repeat_of_invoice_order_and_pack_is_still_a_duplicate(self):
        wizard = self._wizard([
            ('8000000000000031', '2000000000000032', '2000000000000032'),
            ('8000000000000031', '2000000000000032', '2000000000000032'),
        ])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(sorted(batch.line_ids.mapped('status')), ['duplicate', 'pending'])

    def test_row_with_no_id_at_all_is_invalid(self):
        wizard = self._wizard([(None, None, '2000000000000099')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])
        self.assertEqual(batch.line_ids.status, 'invalid')

    def test_already_existing_invoice_is_flagged_without_a_new_job(self):
        existing = self.env['meli.invoice.document'].create({
            'meli_invoice_id': '8000000000000002', 'meli_order_id': '2000000000000002',
        })
        JobModel = self.env['queue.job'].sudo()
        before = JobModel.search_count([
            ('identity_key', '=', 'meli_import_invoice_8000000000000002'),
        ])
        wizard = self._wizard(['8000000000000002'])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'already_existed')
        self.assertEqual(batch.line_ids.document_ids, existing)
        after = JobModel.search_count([
            ('identity_key', '=', 'meli_import_invoice_8000000000000002'),
        ])
        self.assertEqual(before, after)

    def test_new_invoice_is_queued_with_the_shared_identity_key(self):
        wizard = self._wizard(['8000000000000003'])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'pending')
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_invoice_8000000000000003'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'meli.invoice.document')
        self.assertEqual(job.method_name, '_meli_import_invoice_document_for_batch_line')

    def test_jobs_are_staggered_three_seconds_apart(self):
        """The bug this closes: an Excel with many rows used to enqueue
        every job with the same eta (right now), which — combined with
        the since-fixed queue_job_cron_jobrunner retry_pattern bug —
        self-inflicted a burst of API traffic against Mercado Libre
        (production, 2026-09-10). Each successfully-enqueued row must
        now be 3 seconds further out than the previous one.
        """
        wizard = self._wizard([
            '8000000000000010', '8000000000000011',
            '8000000000000012', '8000000000000013',
        ])
        wizard.action_import()

        jobs = self.env['queue.job'].sudo().search([
            ('identity_key', 'in', [
                'meli_import_invoice_8000000000000010',
                'meli_import_invoice_8000000000000011',
                'meli_import_invoice_8000000000000012',
                'meli_import_invoice_8000000000000013',
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
        must not still consume a "slot" in the stagger. Uses 3 real
        rows (not 2) so the comparison lands on the 2nd and 3rd real
        enqueues, both of which get a genuine non-zero eta — the 1st
        real row's own eta=0 is falsy (see the sibling test above), so
        it can't be used in a subtraction here.
        """
        wizard = self._wizard([
            '8000000000000020', 'not-a-number', '8000000000000020',
            '8000000000000021', '8000000000000022',
        ])
        wizard.action_import()

        jobs = self.env['queue.job'].sudo().search([
            ('identity_key', 'in', [
                'meli_import_invoice_8000000000000020',
                'meli_import_invoice_8000000000000021',
                'meli_import_invoice_8000000000000022',
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

    # -- merged Order ID / Pack ID columns (2026-09-14) --

    def test_order_id_with_no_document_yet_is_reported_not_found(self):
        wizard = self._wizard([(None, '7000000000000030', '2000000000000030')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])
        self.assertEqual(batch.line_ids.status, 'not_found')

    def test_order_and_pack_id_relate_an_existing_document_to_its_sale(self):
        order = self._adopted_order('2000000000000031')
        document = self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000031', 'transaction_type': 'sale',
        })
        self.assertFalse(document.sale_order_id)

        wizard = self._wizard([(None, '7000000000000031', '2000000000000031')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'related')
        self.assertEqual(batch.line_ids.document_ids, document)
        self.assertEqual(document.sale_order_id, order)

    def test_pack_id_known_but_no_order_resolves_it_is_still_orphan(self):
        document = self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000032', 'transaction_type': 'sale',
        })
        wizard = self._wizard([(None, '7000000000000032', '2000000000000032')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'still_orphan')
        self.assertEqual(document.meli_linked_pack_id, '2000000000000032')
        self.assertFalse(document.sale_order_id)

    def test_every_document_sharing_the_order_id_is_updated(self):
        order = self._adopted_order('2000000000000033')
        invoice_doc = self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000033', 'transaction_type': 'sale',
        })
        credit_note_doc = self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000033', 'transaction_type': 'devolution',
        })

        wizard = self._wizard([(None, '7000000000000033', '2000000000000033')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'related')
        self.assertEqual(
            set(batch.line_ids.document_ids.ids), {invoice_doc.id, credit_note_doc.id},
        )
        self.assertEqual(invoice_doc.sale_order_id, order)
        self.assertEqual(credit_note_doc.sale_order_id, order)

    def test_order_id_alone_without_pack_id_just_reports_already_existed(self):
        self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000034', 'transaction_type': 'sale',
        })
        wizard = self._wizard([(None, '7000000000000034', None)])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])
        self.assertEqual(batch.line_ids.status, 'already_existed')

    def test_status_is_never_written_by_a_pack_only_row(self):
        self._adopted_order('2000000000000035')
        document = self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000035', 'transaction_type': 'sale',
            'status': 'authorized',
        })
        wizard = self._wizard([(None, '7000000000000035', '2000000000000035')])
        wizard.action_import()
        self.assertEqual(document.status, 'authorized')

    def test_existing_invoice_with_pack_id_is_related_synchronously(self):
        """An Invoice ID that already resolves to a document (no new API
        fetch needed) applies its Pack ID right away in the wizard —
        no queue_job involved for this part.
        """
        order = self._adopted_order('2000000000000036')
        existing = self.env['meli.invoice.document'].create({
            'meli_invoice_id': '8000000000000036', 'meli_order_id': '7000000000000036',
            'transaction_type': 'sale',
        })
        wizard = self._wizard([('8000000000000036', '7000000000000036', '2000000000000036')])
        action = wizard.action_import()
        batch = self.env['meli.invoice.import.batch'].browse(action['res_id'])

        self.assertEqual(batch.line_ids.status, 'related')
        self.assertEqual(existing.sale_order_id, order)

    def test_reimporting_the_same_pack_mapping_stays_related(self):
        self._adopted_order('2000000000000037')
        self.env['meli.invoice.document'].create({
            'meli_order_id': '7000000000000037', 'transaction_type': 'sale',
        })
        self._wizard([(None, '7000000000000037', '2000000000000037')]).action_import()
        second_action = self._wizard(
            [(None, '7000000000000037', '2000000000000037')]
        ).action_import()
        second_batch = self.env['meli.invoice.import.batch'].browse(second_action['res_id'])
        self.assertEqual(second_batch.line_ids.status, 'related')


@tagged('post_install', '-at_install')
class TestMeliImportInvoiceDocumentForBatchLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice import batch line)'})
        cls.partner = cls.env['res.partner'].create({'name': 'Meli Invoice Batch Line Test Buyer'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'invoice-batch-line-client', 'client_secret': 'invoice-batch-line-secret',
            'state': 'connected', 'ml_user_id': '999',
            'partner_id': cls.partner.id,
        })
        cls.warehouse = cls.env['stock.warehouse'].create({
            'name': 'Almacén Invoice Batch Line Test', 'code': 'IBLT',
            'company_id': cls.test_company.id,
        })

    def _line(self, invoice_id, pack_id=False):
        batch = self.env['meli.invoice.import.batch'].create({'company_id': self.test_company.id})
        return self.env['meli.invoice.import.batch.line'].create({
            'batch_id': batch.id, 'invoice_id': invoice_id, 'pack_id': pack_id,
            'status': 'pending',
        })

    def test_success_marks_line_imported_with_the_document(self):
        line = self._line('9000000000000001')
        with patch.object(type(self.config), '_api_get', return_value={}), \
             patch.object(type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>'):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                self.test_company.id, '9000000000000001', line.id,
            )
        self.assertEqual(line.status, 'imported')
        self.assertEqual(line.document_ids.meli_invoice_id, '9000000000000001')

    def test_invoice_id_mercado_libre_never_heard_of_marks_line_not_found(self):
        line = self._line('9000000000000002')
        # Both the metadata call and every XML fetch 404 — exactly a
        # mistyped/nonexistent invoice_id, which _meli_import_invoice_document
        # itself already tolerates by returning a document without a file.
        with patch.object(type(self.config), '_api_get', return_value={}), \
             patch.object(type(self.config), '_api_get_raw', side_effect=self._http_404()):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                self.test_company.id, '9000000000000002', line.id,
            )
        self.assertEqual(line.status, 'not_found')

    def test_exception_marks_line_error_without_raising(self):
        line = self._line('9000000000000003')
        with patch.object(
            type(self.env['meli.invoice.document']), '_meli_import_invoice_document',
            side_effect=Exception('boom'),
        ):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                self.test_company.id, '9000000000000003', line.id,
            )
        self.assertEqual(line.status, 'error')
        self.assertTrue(line.message)

    def test_retryable_job_error_propagates_instead_of_being_swallowed(self):
        """Fix 5 (2026-09-09, final review — Important): RetryableJobError
        is itself an Exception subclass — before this fix, the generic
        `except Exception` here caught it too and permanently marked the
        batch line 'error', defeating queue_job's own retry machinery on
        exactly the bulk-import path most likely to hit Mercado Libre
        rate limiting. It must propagate unchanged instead.
        """
        line = self._line('9000000000000006')
        with patch.object(
            type(self.env['meli.invoice.document']), '_meli_import_invoice_document',
            side_effect=RetryableJobError('transient, please retry'),
        ):
            with self.assertRaises(RetryableJobError):
                self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                    self.test_company.id, '9000000000000006', line.id,
                )
        self.assertEqual(
            line.status, 'pending',
            "the line must NOT have been marked 'error' — the exception "
            "must propagate to queue_job's own retry machinery instead",
        )

    def test_retry_pending_reenqueues_only_non_final_lines(self):
        batch = self.env['meli.invoice.import.batch'].create({'company_id': self.test_company.id})
        pending = self.env['meli.invoice.import.batch.line'].create({
            'batch_id': batch.id, 'invoice_id': '9000000000000004', 'status': 'error',
            'message': 'boom',
        })
        done = self.env['meli.invoice.import.batch.line'].create({
            'batch_id': batch.id, 'invoice_id': '9000000000000005', 'status': 'imported',
        })
        JobModel = self.env['queue.job'].sudo()
        before = JobModel.search_count([
            ('identity_key', '=', 'meli_import_invoice_9000000000000004'),
        ])
        batch.action_retry_pending()
        self.assertEqual(pending.status, 'pending')
        self.assertEqual(done.status, 'imported')
        after = JobModel.search_count([
            ('identity_key', '=', 'meli_import_invoice_9000000000000004'),
        ])
        self.assertEqual(after, before + 1)

    def test_retry_pending_skips_order_only_lines_with_no_invoice_id(self):
        """A line that only ever had an Order ID (no Invoice ID at all)
        has nothing to fetch from the API — retrying it would just
        immediately fail with an empty invoice_id.
        """
        batch = self.env['meli.invoice.import.batch'].create({'company_id': self.test_company.id})
        order_only = self.env['meli.invoice.import.batch.line'].create({
            'batch_id': batch.id, 'order_id': '7000000000000040',
            'pack_id': '2000000000000040', 'status': 'not_found',
        })
        JobModel = self.env['queue.job'].sudo()
        before = JobModel.search_count([('model_name', '=', 'meli.invoice.document')])
        batch.action_retry_pending()
        self.assertEqual(order_only.status, 'not_found')
        after = JobModel.search_count([('model_name', '=', 'meli.invoice.document')])
        self.assertEqual(before, after)

    def test_pack_id_applied_after_a_new_invoice_is_imported(self):
        """The Pack ID from the same Excel row is passed through to the
        job so it applies once the document actually exists — it can't
        be applied synchronously in the wizard for a brand-new
        Invoice ID (see meli_invoice_import_batch_wizard.action_import).
        """
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'reference': '2000000000000041', 'client_order_ref': '2000000000000041',
            'meli_pack_id': '2000000000000041',
        })
        line = self._line('9000000000000007', pack_id='2000000000000041')
        with patch.object(type(self.config), '_api_get', return_value={}), \
             patch.object(type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>'):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                self.test_company.id, '9000000000000007', line.id, pack_id='2000000000000041',
            )
        self.assertEqual(line.status, 'related')
        self.assertEqual(line.document_ids.sale_order_id, order)

    @staticmethod
    def _http_404():
        import requests
        from unittest.mock import MagicMock

        def _raise(*args, **kwargs):
            response = MagicMock()
            response.status_code = 404
            raise requests.exceptions.HTTPError(response=response)
        return _raise
