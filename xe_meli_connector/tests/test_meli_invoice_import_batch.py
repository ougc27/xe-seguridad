import base64
import io
from unittest.mock import patch

import openpyxl

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..wizards.meli_invoice_import_batch_wizard import _meli_extract_invoice_ids


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
class TestMeliExtractInvoiceIds(TransactionCase):
    """Pure parsing logic — no Odoo records involved."""

    def test_valid_numeric_text_and_float_cells_are_recognized(self):
        content = _make_xlsx([
            'invoice_id',  # header row, not a valid invoice id
            '3000000083516040',  # text
            3000000083516041,  # int
            3000000083516042.0,  # float, whole number
        ])
        rows = _meli_extract_invoice_ids(content)
        self.assertEqual(rows, [
            ('invoice_id', None),
            ('3000000083516040', '3000000083516040'),
            ('3000000083516041', '3000000083516041'),
            ('3000000083516042', '3000000083516042'),
        ])

    def test_blank_rows_are_skipped_entirely(self):
        content = _make_xlsx(['3000000083516040', None, '3000000083516041'])
        rows = _meli_extract_invoice_ids(content)
        self.assertEqual(len(rows), 2)

    def test_non_numeric_text_is_reported_as_invalid(self):
        content = _make_xlsx(['not-an-invoice-id'])
        rows = _meli_extract_invoice_ids(content)
        self.assertEqual(rows, [('not-an-invoice-id', None)])


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

    def _wizard(self, xlsx_rows):
        content = _make_xlsx(xlsx_rows)
        return self.env['meli.invoice.import.batch.wizard'].with_company(
            self.test_company
        ).create({
            'excel_file': base64.b64encode(content),
            'filename': 'invoices.xlsx',
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
        self.assertEqual(batch.line_ids.document_id, existing)
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

    def _line(self, invoice_id):
        batch = self.env['meli.invoice.import.batch'].create({'company_id': self.test_company.id})
        return self.env['meli.invoice.import.batch.line'].create({
            'batch_id': batch.id, 'invoice_id': invoice_id, 'status': 'pending',
        })

    def test_success_marks_line_imported_with_the_document(self):
        line = self._line('9000000000000001')
        with patch.object(type(self.config), '_api_get', return_value={}), \
             patch.object(type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>'):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_batch_line(
                self.test_company.id, '9000000000000001', line.id,
            )
        self.assertEqual(line.status, 'imported')
        self.assertEqual(line.document_id.meli_invoice_id, '9000000000000001')

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

    @staticmethod
    def _http_404():
        import requests
        from unittest.mock import MagicMock

        def _raise(*args, **kwargs):
            response = MagicMock()
            response.status_code = 404
            raise requests.exceptions.HTTPError(response=response)
        return _raise
