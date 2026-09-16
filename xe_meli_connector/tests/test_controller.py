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

    def test_orders_v2_notification_is_ignored(self):
        # Invoicing-only build (2026-09-14): the sale.order injector is
        # deliberately not wired up — an 'orders_v2' notification must
        # never enqueue an import job.
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
        self.assertFalse(job)

    def test_post_purchase_notification_is_ignored(self):
        # meli.claim isn't installed in this build — must never even
        # try to reach it.
        response = self._post_notification({
            'topic': 'post_purchase',
            'resource': 'post-purchase/v1/claims/5999999999',
            'user_id': 777777777,
            'actions': ['claims'],
        })
        self.assertEqual(response.status_code, 200)

    def test_notification_for_other_topic_is_ignored(self):
        response = self._post_notification({
            'topic': 'items',
            'resource': '/items/MLM123',
            'user_id': 777777777,
            'attempts': 1,
        })
        self.assertEqual(response.status_code, 200)

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
