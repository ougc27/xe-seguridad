import json

import odoo
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestMeliCallbackController(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_user = cls.env['res.users'].create({
            'name': 'Meli Test Admin',
            'login': 'meli_test_admin',
            'password': 'meli_test_admin_pwd',
            'groups_id': [(6, 0, [cls.env.ref('base.group_system').id])],
        })
        # A dedicated test company, never `env.company` — avoids colliding
        # with a real meli.config already in this shared database.
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli controller)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'ml_user_id': '555555555',
        })

    def test_callback_without_session_state_is_rejected(self):
        self.authenticate('meli_test_admin', 'meli_test_admin_pwd')
        response = self.url_open('/meli/callback?code=CODE-1&state=whatever')
        self.assertEqual(response.status_code, 400)

    def test_callback_with_mismatched_state_is_rejected(self):
        self.authenticate('meli_test_admin', 'meli_test_admin_pwd')
        self.session['meli_oauth_state'] = 'expected-state'
        self.session['meli_oauth_verifier'] = 'some-verifier'
        self.session['meli_oauth_config_id'] = self.config.id
        odoo.http.root.session_store.save(self.session)
        response = self.url_open(
            '/meli/callback?code=CODE-1&state=different-state'
        )
        self.assertEqual(response.status_code, 400)

    def test_callback_without_login_is_not_processed(self):
        response = self.url_open(
            '/meli/callback?code=CODE-1&state=whatever',
            allow_redirects=False,
        )
        self.assertNotEqual(response.status_code, 200)


@tagged('post_install', '-at_install')
class TestMeliNotificationsController(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli notifications)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'ml_user_id': '777777777',
        })

    def _post_notification(self, payload):
        return self.url_open(
            '/meli/notifications',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
        )

    def test_orders_v2_notification_enqueues_import_job(self):
        response = self._post_notification({
            'topic': 'orders_v2',
            'resource': '/orders/2000099999999999',
            'user_id': 777777777,
            'attempts': 1,
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_order_2000099999999999'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'sale.order')
        self.assertEqual(job.method_name, '_meli_import_order')

    def test_notification_for_unknown_user_id_is_ignored(self):
        response = self._post_notification({
            'topic': 'orders_v2',
            'resource': '/orders/2000099999999998',
            'user_id': 111111111,
            'attempts': 1,
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_order_2000099999999998'),
        ])
        self.assertFalse(job)

    def test_notification_for_other_topic_is_ignored(self):
        # Scoped by count delta, not absolute absence: this suite runs
        # against a shared database where other tests may have already
        # created unrelated _meli_import_order jobs.
        JobModel = self.env['queue.job'].sudo()
        domain = [('model_name', '=', 'sale.order'), ('method_name', '=', '_meli_import_order')]
        count_before = JobModel.search_count(domain)

        response = self._post_notification({
            'topic': 'items',
            'resource': '/items/MLM123',
            'user_id': 777777777,
            'attempts': 1,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(JobModel.search_count(domain), count_before)

    def test_post_purchase_notification_enqueues_claim_import_job(self):
        response = self._post_notification({
            'topic': 'post_purchase',
            'resource': 'post-purchase/v1/claims/5999999999',
            'user_id': 777777777,
            'actions': ['claims'],
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_claim_5999999999'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'meli.claim')
        self.assertEqual(job.method_name, '_meli_import_claim')

    def test_post_purchase_claims_actions_notification_uses_real_claim_id(self):
        # claims_actions notifications carry a trailing sub-resource
        # segment (e.g. "actions-history") after the claim id — unlike
        # claims notifications, where the claim id is the last segment.
        # Confirmed in production 2026-09-02: taking the last segment
        # unconditionally enqueued/fetched claim id "actions-history"
        # instead of the real numeric claim id.
        response = self._post_notification({
            'topic': 'post_purchase',
            'resource': 'post-purchase/v1/claims/5999999997/actions-history',
            'user_id': 777777777,
            'actions': ['claims_actions'],
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_claim_5999999997'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'meli.claim')
        self.assertEqual(job.method_name, '_meli_import_claim')

    def test_post_purchase_notification_with_unparseable_resource_is_ignored(self):
        JobModel = self.env['queue.job'].sudo()
        domain = [('model_name', '=', 'meli.claim'), ('method_name', '=', '_meli_import_claim')]
        count_before = JobModel.search_count(domain)

        response = self._post_notification({
            'topic': 'post_purchase',
            'resource': '/some/unexpected/path',
            'user_id': 777777777,
            'actions': ['claims'],
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(JobModel.search_count(domain), count_before)

    def test_invoices_notification_enqueues_invoice_document_import_job(self):
        response = self._post_notification({
            'topic': 'invoices',
            'resource': '/users/777777777/invoices/9000000001',
            'user_id': 777777777,
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_invoice_9000000001'),
        ])
        self.assertTrue(job)
        self.assertEqual(job.model_name, 'meli.invoice.document')
        self.assertEqual(job.method_name, '_meli_import_invoice_document')

    def test_invoices_notification_for_unknown_user_id_is_ignored(self):
        response = self._post_notification({
            'topic': 'invoices',
            'resource': '/users/111111111/invoices/9000000002',
            'user_id': 111111111,
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_invoice_9000000002'),
        ])
        self.assertFalse(job)

    def test_post_purchase_notification_for_unknown_user_id_is_ignored(self):
        response = self._post_notification({
            'topic': 'post_purchase',
            'resource': 'post-purchase/v1/claims/5999999998',
            'user_id': 111111111,
            'actions': ['claims'],
        })
        self.assertEqual(response.status_code, 200)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_import_claim_5999999998'),
        ])
        self.assertFalse(job)
