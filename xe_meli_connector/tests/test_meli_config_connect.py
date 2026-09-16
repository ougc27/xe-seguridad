import base64
import hashlib
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import requests

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliConfigConnect(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A dedicated test company, never `env.company` — avoids colliding
        # with a real meli.config already in this shared database.
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli connect)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
        })

    def test_action_connect_builds_authorize_url_with_pkce(self):
        fake_session = {}
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.http_request',
            new=MagicMock()
        ) as mock_request:
            mock_request.session = fake_session
            action = self.config.action_connect()

        self.assertEqual(action['type'], 'ir.actions.act_url')
        parsed = urlparse(action['url'])
        params = parse_qs(parsed.query)
        self.assertEqual(params['client_id'][0], 'test-client-id')
        self.assertEqual(params['response_type'][0], 'code')
        self.assertEqual(params['code_challenge_method'][0], 'S256')
        self.assertEqual(params['state'][0], fake_session['meli_oauth_state'])
        self.assertEqual(fake_session['meli_oauth_config_id'], self.config.id)
        self.assertIn('meli_oauth_verifier', fake_session)

        verifier = fake_session['meli_oauth_verifier']
        expected_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'][0], expected_challenge)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_action_refresh_now_success_notification(self, mock_post):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'access_token': 'A', 'refresh_token': 'R', 'expires_in': 21600,
            'user_id': 1,
        }
        mock_post.return_value = response
        action = self.config.action_refresh_now()
        self.assertEqual(action['params']['type'], 'success')

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_action_refresh_now_error_notification(self, mock_post):
        response = MagicMock()
        response.status_code = 500
        response.text = 'server error'
        response.json.side_effect = ValueError()
        mock_post.return_value = response
        action = self.config.action_refresh_now()
        self.assertEqual(action['params']['type'], 'danger')

    def test_action_toggle_active_flips_the_flag(self):
        self.assertTrue(self.config.active)
        self.config.action_toggle_active()
        self.assertFalse(self.config.active)
        self.config.action_toggle_active()
        self.assertTrue(self.config.active)

    def test_action_open_meli_orders_scopes_domain_to_configured_partners(self):
        partner = self.env['res.partner'].create({'name': 'Meli Orders Partner Test'})
        self.config.partner_id = partner.id

        action = self.env['meli.config'].action_open_meli_orders()

        self.assertEqual(action['res_model'], 'sale.order')
        field, operator, value = action['domain'][0]
        self.assertEqual((field, operator), ('partner_id', 'in'))
        self.assertIn(partner.id, value)

    def test_action_open_meli_orders_includes_inactive_configs(self):
        # Turning the integration off (kill switch) shouldn't hide the
        # historical orders for that partner from the Pedidos view.
        partner = self.env['res.partner'].create({'name': 'Meli Orders Inactive Partner'})
        self.config.partner_id = partner.id
        self.config.action_toggle_active()
        self.assertFalse(self.config.active)

        action = self.env['meli.config'].action_open_meli_orders()

        self.assertIn(partner.id, action['domain'][0][2])

    def test_returns_manager_id_field_exists_and_is_a_user(self):
        manager = self.env['res.users'].create({
            'name': 'Returns Manager Test', 'login': 'returns_manager_test',
        })
        self.config.returns_manager_id = manager
        self.assertEqual(self.config.returns_manager_id, manager)

    def test_shipping_item_id_field_exists_and_is_a_product(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Test Shipping Surcharge', 'type': 'service',
        })
        self.config.shipping_item_id = shipping_item
        self.assertEqual(self.config.shipping_item_id, shipping_item)

    def _connected_config_with_refresh_token(self):
        self.config.write({
            'state': 'connected', 'refresh_token': 'existing-refresh-token',
            'access_token': 'existing-access-token',
            'consecutive_refresh_failures': 0,
        })
        return self.config

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_network_failure_below_threshold_does_not_flip_to_error(self, mock_post):
        config = self._connected_config_with_refresh_token()
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")

        config._refresh_token()

        self.assertEqual(config.state, 'connected')
        self.assertEqual(config.consecutive_refresh_failures, 1)
        self.assertIn('no network', config.last_error)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_network_failure_flips_to_error_after_three_consecutive_failures(self, mock_post):
        config = self._connected_config_with_refresh_token()
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")

        config._refresh_token()
        self.assertEqual(config.state, 'connected')
        config._refresh_token()
        self.assertEqual(config.state, 'connected')
        config._refresh_token()

        self.assertEqual(config.state, 'error')
        # Resets to 0 on escalation (not left at 3): otherwise a still-flaky
        # reconnection attempt made while already 'error' would keep
        # climbing the counter past the threshold forever.
        self.assertEqual(config.consecutive_refresh_failures, 0)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_successful_refresh_resets_the_failure_counter(self, mock_post):
        config = self._connected_config_with_refresh_token()
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")
        config._refresh_token()
        config._refresh_token()
        self.assertEqual(config.consecutive_refresh_failures, 2)

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'access_token': 'NEW', 'refresh_token': 'NEW-R', 'expires_in': 21600,
            'user_id': 1,
        }
        mock_post.side_effect = None
        mock_post.return_value = response
        config._refresh_token()

        self.assertEqual(config.state, 'connected')
        self.assertEqual(config.consecutive_refresh_failures, 0)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_http_rejection_flips_to_error_immediately_ignoring_threshold(self, mock_post):
        config = self._connected_config_with_refresh_token()
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {'error': 'invalid_grant'}
        mock_post.return_value = response

        config._refresh_token()

        self.assertEqual(config.state, 'error')
        self.assertEqual(config.consecutive_refresh_failures, 0)

    def _admin_user(self):
        return self.env['res.users'].create({
            'name': 'Meli Admin Test', 'login': 'meli_admin_disconnect_test',
            'groups_id': [(4, self.env.ref('xe_meli_connector.group_meli_admin').id)],
        })

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_reaching_error_via_threshold_notifies_group_meli_admin(self, mock_post):
        admin = self._admin_user()
        config = self._connected_config_with_refresh_token()
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")

        config._refresh_token()
        config._refresh_token()
        config._refresh_token()

        self.assertEqual(config.state, 'error')
        message = config.message_ids[0]
        self.assertIn(admin.partner_id.id, message.partner_ids.ids)
        self.assertIn('Error', message.body)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_below_threshold_does_not_notify_yet(self, mock_post):
        self._admin_user()
        config = self._connected_config_with_refresh_token()
        # self.config/self.test_company is a class-level fixture shared by
        # every test in this class (see setUpClass) — an earlier test
        # (e.g. test_action_refresh_now_error_notification) may already
        # have posted a chatter message to it. Compare against a baseline
        # instead of asserting "no messages ever", since this test only
        # cares that ITS OWN below-threshold failures didn't add one.
        messages_before = config.message_ids
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")

        config._refresh_token()
        config._refresh_token()

        self.assertEqual(config.state, 'connected')
        self.assertEqual(config.message_ids, messages_before)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_http_rejection_notifies_group_meli_admin_immediately(self, mock_post):
        admin = self._admin_user()
        config = self._connected_config_with_refresh_token()
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {'error': 'invalid_grant'}
        mock_post.return_value = response

        config._refresh_token()

        self.assertEqual(config.state, 'error')
        message = config.message_ids[0]
        self.assertIn(admin.partner_id.id, message.partner_ids.ids)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_does_not_renotify_admins_while_already_in_error(self, mock_post):
        # At 1,100+ orders/day, _api_request retries any 401 by calling
        # _refresh_token() regardless of state — without this guard, a
        # genuinely revoked token would re-mention every admin on every
        # single queued job, or on every "Refresh token now" retry.
        self._admin_user()
        config = self._connected_config_with_refresh_token()
        mock_post.side_effect = requests.exceptions.ConnectionError("no network")
        config._refresh_token()
        config._refresh_token()
        config._refresh_token()
        self.assertEqual(config.state, 'error')
        messages_after_first_error = config.message_ids

        config._refresh_token()

        self.assertEqual(config.state, 'error')
        self.assertEqual(config.message_ids, messages_after_first_error)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_http_rejection_does_not_renotify_when_already_in_error(self, mock_post):
        self._admin_user()
        config = self._connected_config_with_refresh_token()
        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {'error': 'invalid_grant'}
        mock_post.return_value = response
        config._refresh_token()
        self.assertEqual(config.state, 'error')
        messages_after_first_error = config.message_ids

        config._refresh_token()

        self.assertEqual(config.state, 'error')
        self.assertEqual(config.message_ids, messages_after_first_error)

@tagged('post_install', '-at_install')
class TestMeliConfigApiRaw(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli api raw)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'raw-client', 'client_secret': 'raw-secret',
            'access_token': 'raw-access-token', 'state': 'connected',
        })

    def _fake_response(self, status_code=200, content=b''):
        response = MagicMock()
        response.status_code = status_code
        response.content = content
        response.raise_for_status = MagicMock()
        return response

    def test_api_get_raw_returns_bytes_not_json(self):
        xml_bytes = b'<cfdi:Comprobante></cfdi:Comprobante>'
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            return_value=self._fake_response(content=xml_bytes),
        ) as mock_request:
            result = self.config._api_get_raw('/invoices/io/documents/stream/invoice/123/xml')

        self.assertEqual(result, xml_bytes)
        mock_request.assert_called_once()
        self.assertEqual(mock_request.call_args.args[0], 'GET')

    def test_api_get_raw_retries_once_after_401_refresh(self):
        unauthorized = self._fake_response(status_code=401, content=b'')
        ok = self._fake_response(content=b'<xml/>')
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            side_effect=[unauthorized, ok],
        ):
            with patch.object(type(self.config), '_refresh_token') as mock_refresh:
                mock_refresh.side_effect = lambda: self.config.write({'state': 'connected'})
                result = self.config._api_get_raw('/invoices/io/documents/stream/invoice/123/xml')

        self.assertEqual(result, b'<xml/>')
