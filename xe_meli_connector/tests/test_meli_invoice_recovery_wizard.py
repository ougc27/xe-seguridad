from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliInvoiceRecoveryWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice recovery)'})
        cls.partner = cls.env['res.partner'].create({'name': 'Meli Invoice Recovery Buyer'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'recovery-client', 'client_secret': 'recovery-secret',
            'state': 'connected', 'ml_user_id': '999',
            'partner_id': cls.partner.id,
        })

    def _wizard(self, **vals):
        return self.env['meli.invoice.recovery.wizard'].with_company(
            self.test_company
        ).create({
            'date_from': fields.Datetime.subtract(fields.Datetime.now(), hours=24),
            'date_to': fields.Datetime.now(),
            **vals,
        })

    def _recovery_jobs(self):
        return self.env['queue.job'].sudo().search([
            ('model_name', '=', 'meli.invoice.document'),
            ('method_name', '=', '_meli_recover_invoices_in_range'),
        ])

    def test_recover_enqueues_a_single_job_without_touching_the_api(self):
        # action_recover() must not enumerate orders or call the API
        # itself — that's the whole point of moving the work to a job
        # (found in practice 2026-09-08: a loop of with_delay() calls
        # per order, run inside the request, made the wizard hang for
        # minutes over a real date range).
        with patch.object(type(self.config), '_api_get_raw') as mocked_raw:
            result = self._wizard().action_recover()

        mocked_raw.assert_not_called()
        jobs = self._recovery_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(result['tag'], 'display_notification')

    def test_recover_job_identity_key_scopes_by_company_and_range(self):
        date_from = fields.Datetime.subtract(fields.Datetime.now(), hours=48)
        date_to = fields.Datetime.now()
        self._wizard(date_from=date_from, date_to=date_to).action_recover()

        job = self._recovery_jobs()
        self.assertEqual(len(job), 1)
        self.assertIn(str(self.test_company.id), job.identity_key)
        self.assertIn(str(date_from), job.identity_key)
        self.assertIn(str(date_to), job.identity_key)

    def test_recover_without_active_connection_raises(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli connection, invoices)'})
        wizard = self.env['meli.invoice.recovery.wizard'].with_company(
            other_company
        ).create({
            'date_from': fields.Datetime.subtract(fields.Datetime.now(), hours=24),
            'date_to': fields.Datetime.now(),
        })
        with self.assertRaises(UserError):
            wizard.action_recover()

    def test_date_from_must_be_before_date_to(self):
        with self.assertRaises(Exception):
            self._wizard(
                date_from=fields.Datetime.now(),
                date_to=fields.Datetime.subtract(fields.Datetime.now(), hours=1),
            )
