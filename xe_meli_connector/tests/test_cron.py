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
class TestMeliConfigPolling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli polling)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.company.id,
            'client_id': 'poll-client', 'client_secret': 'poll-secret',
            'state': 'connected', 'ml_user_id': '999',
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Meli Poll Test',
            # A base_automation rule blocks a sale.order from reaching
            # state='sale' unless the partner's x_cop (Studio field,
            # label "Tipo") is set — matches the real MERCADO LIBRE
            # partner's configuration.
            'x_cop': 'cliente',
        })
        cls.warehouse = cls.env['stock.warehouse'].create({
            'name': 'Almacén Poll Test', 'code': 'POLT',
            'company_id': cls.company.id,
        })

    def _job_identity_keys(self):
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'sale.order'), ('method_name', '=', '_meli_import_order'),
        ])
        return set(jobs.mapped('identity_key'))

    def test_poll_skips_confirmed_orders_but_retries_stuck_drafts(self):
        confirmed = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'client_order_ref': 'POLL-CONFIRMED',
            'meli_order_id': 'POLL-CONFIRMED',
            'company_id': self.company.id, 'warehouse_id': self.warehouse.id,
            'meli_sync_source': 'xe_meli_connector', 'state': 'sale',
        })
        stuck_draft = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'client_order_ref': 'POLL-STUCK-DRAFT',
            'meli_order_id': 'POLL-STUCK-DRAFT',
            'company_id': self.company.id, 'warehouse_id': self.warehouse.id,
            'meli_sync_source': 'xe_meli_connector', 'state': 'draft',
        })
        with patch.object(
            type(self.config), '_api_get',
            return_value={'results': [
                {'id': 'POLL-CONFIRMED'},
                {'id': 'POLL-STUCK-DRAFT'},
                {'id': 'POLL-BRAND-NEW'},
            ]},
        ):
            self.config._poll_recent_orders()

        keys = self._job_identity_keys()
        self.assertNotIn('meli_import_order_POLL-CONFIRMED', keys)
        self.assertIn('meli_import_order_POLL-STUCK-DRAFT', keys)
        self.assertIn('meli_import_order_POLL-BRAND-NEW', keys)

    def test_first_poll_ever_falls_back_to_default_lookback(self):
        self.assertFalse(self.config.last_poll_at)
        with patch.object(
            type(self.config), '_api_get', return_value={'results': []},
        ) as mocked_get:
            self.config._poll_recent_orders()

        params = mocked_get.call_args.kwargs['params']
        # No checkpoint yet: falls back to the default 2h lookback, plus
        # the overlap margin. The API only supports hour granularity, so
        # the code truncates minutes to :00 — compare on that same basis.
        expected_since = fields.Datetime.subtract(
            fields.Datetime.now(), hours=2, minutes=15,
        )
        self.assertEqual(
            params['order.date_last_updated.from'],
            expected_since.strftime('%Y-%m-%dT%H:00:00.000-00:00'),
        )
        self.assertTrue(self.config.last_poll_at)

    def test_poll_resumes_from_last_checkpoint_not_a_fixed_window(self):
        # An outage far longer than the fixed 2h default (here: 30h) must
        # still be fully covered from the checkpoint, proving there is no
        # permanent gap regardless of how long the webhook was down.
        old_checkpoint = fields.Datetime.subtract(fields.Datetime.now(), hours=30)
        self.config.last_poll_at = old_checkpoint
        expected_since = fields.Datetime.subtract(old_checkpoint, minutes=15)
        with patch.object(
            type(self.config), '_api_get', return_value={'results': []},
        ) as mocked_get:
            self.config._poll_recent_orders()

        params = mocked_get.call_args.kwargs['params']
        self.assertEqual(
            params['order.date_last_updated.from'],
            expected_since.strftime('%Y-%m-%dT%H:00:00.000-00:00'),
        )

    def test_successful_poll_advances_the_checkpoint(self):
        self.config.last_poll_at = fields.Datetime.subtract(fields.Datetime.now(), hours=10)
        before = fields.Datetime.now()
        with patch.object(
            type(self.config), '_api_get', return_value={'results': []},
        ):
            self.config._poll_recent_orders()

        self.assertGreaterEqual(self.config.last_poll_at, before)

    def test_failed_poll_does_not_advance_the_checkpoint(self):
        checkpoint = fields.Datetime.subtract(fields.Datetime.now(), hours=10)
        self.config.last_poll_at = checkpoint
        with patch.object(
            type(self.config), '_api_get', side_effect=Exception("ML is down"),
        ):
            with self.assertRaises(Exception):
                self.config._poll_recent_orders()

        self.assertEqual(self.config.last_poll_at, checkpoint)


@tagged('post_install', '-at_install')
class TestMeliConfigClaimPolling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli claim polling)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.company.id,
            'client_id': 'poll-claim-client', 'client_secret': 'poll-claim-secret',
            'state': 'connected', 'ml_user_id': '999',
        })

    def _job_identity_keys(self):
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'meli.claim'), ('method_name', '=', '_meli_import_claim'),
        ])
        return set(jobs.mapped('identity_key'))

    def test_claims_poll_cron_interval_is_thirty_minutes(self):
        cron = self.env.ref('xe_meli_connector.ir_cron_meli_poll_claims')
        self.assertEqual(cron.interval_number, 30)
        self.assertEqual(cron.interval_type, 'minutes')

    def test_poll_recent_claims_queues_every_result_and_advances_checkpoint(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'data': [{'id': 5000000101}, {'id': 5000000102}],
                'paging': {'total': 2},
            },
        ):
            self.config._poll_recent_claims()

        keys = self._job_identity_keys()
        self.assertIn('meli_import_claim_5000000101', keys)
        self.assertIn('meli_import_claim_5000000102', keys)
        self.assertTrue(self.config.last_claim_poll_at)

    def test_first_claim_poll_ever_uses_seller_and_default_lookback(self):
        self.assertFalse(self.config.last_claim_poll_at)
        with patch.object(
            type(self.config), '_api_get',
            return_value={'data': [], 'paging': {'total': 0}},
        ) as mocked_get:
            self.config._poll_recent_claims()

        params = mocked_get.call_args.kwargs['params']
        self.assertEqual(params['players.user_id'], '999')
        self.assertEqual(params['players.role'], 'respondent')
        # Required even though it isn't conceptually a filter for this
        # query — confirmed against the real API (2026-09-04) that
        # range/players.* alone don't satisfy Mercado Libre's own
        # "atLeastOneFilterProvided" validation (HTTP 400 otherwise).
        self.assertEqual(params['site_id'], 'MLM')
        self.assertIn('last_updated:after:', params['range'])
        self.assertTrue(self.config.last_claim_poll_at)

    def test_failed_claim_poll_does_not_advance_the_checkpoint(self):
        checkpoint = fields.Datetime.subtract(fields.Datetime.now(), hours=10)
        self.config.last_claim_poll_at = checkpoint
        with patch.object(
            type(self.config), '_api_get', side_effect=Exception("ML is down"),
        ):
            with self.assertRaises(Exception):
                self.config._poll_recent_claims()

        self.assertEqual(self.config.last_claim_poll_at, checkpoint)

    def test_search_claim_ids_paginates_until_total_reached(self):
        page_1 = {
            'data': [{'id': 1000000 + i} for i in range(100)],
            'paging': {'total': 150},
        }
        page_2 = {
            'data': [{'id': 1000100 + i} for i in range(50)],
            'paging': {'total': 150},
        }
        with patch.object(
            type(self.config), '_api_get', side_effect=[page_1, page_2],
        ) as mocked_get:
            claim_ids = self.config._meli_search_claim_ids(
                fields.Datetime.subtract(fields.Datetime.now(), hours=1),
                fields.Datetime.now(),
            )

        self.assertEqual(len(claim_ids), 150)
        self.assertEqual(mocked_get.call_count, 2)

    def test_search_claim_ids_stops_on_empty_page_even_if_total_looks_bigger(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value={'data': [], 'paging': {'total': 500}},
        ) as mocked_get:
            claim_ids = self.config._meli_search_claim_ids(
                fields.Datetime.subtract(fields.Datetime.now(), hours=1),
                fields.Datetime.now(),
            )

        self.assertEqual(claim_ids, [])
        self.assertEqual(mocked_get.call_count, 1)


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
