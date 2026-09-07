from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliClaimRecoveryWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli claim recovery)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'recovery-client', 'client_secret': 'recovery-secret',
            'state': 'connected', 'ml_user_id': '999',
        })

    def _job_identity_keys(self):
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'meli.claim'), ('method_name', '=', '_meli_import_claim'),
        ])
        return set(jobs.mapped('identity_key'))

    def _wizard(self, **vals):
        return self.env['meli.claim.recovery.wizard'].with_company(
            self.test_company
        ).create({
            'date_from': fields.Datetime.subtract(fields.Datetime.now(), hours=24),
            'date_to': fields.Datetime.now(),
            **vals,
        })

    def test_recover_queues_every_claim_found_in_range(self):
        wizard = self._wizard()
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'data': [{'id': 6000000201}, {'id': 6000000202}],
                'paging': {'total': 2},
            },
        ):
            result = wizard.action_recover()

        keys = self._job_identity_keys()
        self.assertIn('meli_import_claim_6000000201', keys)
        self.assertIn('meli_import_claim_6000000202', keys)
        self.assertEqual(result['tag'], 'display_notification')
        self.assertIn('2', result['params']['message'])

    def test_recover_without_active_connection_raises(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli connection, claims)'})
        wizard = self.env['meli.claim.recovery.wizard'].with_company(
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
