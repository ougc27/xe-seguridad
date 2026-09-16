import unittest
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliConfigCron(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Two new test companies, skipping the product pricelist cascade
        # (which otherwise triggers xe_pacific's unconditional archive/unarchive
        # restriction). Neither is `env.company` — this suite runs against the
        # team's real shared database, which may already have a real
        # meli.config for the real companies (company_id is unique).
        cls.company_1 = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co 1'})
        cls.company_2 = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co 2'})

        cls.config_ok = cls.env['meli.config'].create({
            'company_id': cls.company_1.id,
            'client_id': 'ok-client', 'client_secret': 'ok-secret',
            'refresh_token': 'ok-refresh', 'state': 'connected',
        })
        cls.config_broken = cls.env['meli.config'].create({
            'company_id': cls.company_2.id,
            'client_id': 'broken-client', 'client_secret': 'broken-secret',
            'refresh_token': 'broken-refresh', 'state': 'connected',
        })

    def test_cron_isolates_failures_per_record(self):
        def side_effect(record_self):
            if record_self.id == self.config_broken.id:
                raise Exception("boom")
            record_self.write({'access_token': 'REFRESHED'})

        with patch.object(
            type(self.config_ok), '_refresh_token', autospec=True,
            side_effect=side_effect,
        ):
            # Must not raise: the cron isolates each record's failure.
            self.env['meli.config']._cron_refresh_tokens()

        self.assertEqual(self.config_ok.access_token, 'REFRESHED')
        self.assertFalse(self.config_broken.access_token)

    def test_refresh_cron_interval_is_two_hours(self):
        cron = self.env.ref('xe_meli_connector.ir_cron_meli_refresh_tokens')
        self.assertEqual(cron.interval_number, 2)
        self.assertEqual(cron.interval_type, 'hours')


@tagged('post_install', '-at_install')
class TestMeliConfigInvoicePolling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice polling)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.company.id,
            'client_id': 'poll-invoice-app-id', 'client_secret': 'poll-invoice-secret',
            'state': 'connected', 'ml_user_id': '999',
        })

    def _job_identity_keys(self):
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'meli.invoice.document'),
            ('method_name', '=', '_meli_import_invoice_document'),
        ])
        return set(jobs.mapped('identity_key'))

    def _missed_feeds_page(self, invoice_ids):
        return {'messages': [
            {'resource': f'/users/999/invoices/{invoice_id}'}
            for invoice_id in invoice_ids
        ]}

    def test_invoice_poll_cron_interval_is_thirty_minutes(self):
        cron = self.env.ref('xe_meli_connector.ir_cron_meli_poll_invoices')
        self.assertEqual(cron.interval_number, 30)
        self.assertEqual(cron.interval_type, 'minutes')

    def test_missed_invoice_ids_uses_client_id_as_app_id(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._missed_feeds_page([]),
        ) as mocked_get:
            self.config._meli_missed_invoice_ids()

        params = mocked_get.call_args.kwargs['params']
        self.assertEqual(params['app_id'], 'poll-invoice-app-id')
        self.assertEqual(params['topic'], 'invoices')

    def test_missed_invoice_ids_paginates_until_a_short_page(self):
        full_page = self._missed_feeds_page([str(9000000000000000 + i) for i in range(100)])
        short_page = self._missed_feeds_page(['9100000000000001'])
        with patch.object(
            type(self.config), '_api_get', side_effect=[full_page, short_page],
        ) as mocked_get:
            invoice_ids = self.config._meli_missed_invoice_ids()

        self.assertEqual(len(invoice_ids), 101)
        self.assertEqual(mocked_get.call_count, 2)

    def test_poll_recent_invoices_queues_every_missed_invoice(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._missed_feeds_page(['9200000000000001', '9200000000000002']),
        ):
            self.config._poll_recent_invoices()

        keys = self._job_identity_keys()
        self.assertIn('meli_import_invoice_9200000000000001', keys)
        self.assertIn('meli_import_invoice_9200000000000002', keys)

    def test_poll_recent_invoices_skips_already_known_invoice_ids(self):
        # Mercado Libre keeps listing a missed notification for its full
        # 2-day retention window even after we've already imported it —
        # re-enqueueing it every 30 minutes for 2 days would be wasteful.
        self.env['meli.invoice.document'].create({
            'meli_order_id': '9200000000000003', 'meli_invoice_id': '9200000000000003',
            'transaction_type': 'sale',
        })
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._missed_feeds_page(['9200000000000003']),
        ):
            self.config._poll_recent_invoices()

        keys = self._job_identity_keys()
        self.assertNotIn('meli_import_invoice_9200000000000003', keys)
