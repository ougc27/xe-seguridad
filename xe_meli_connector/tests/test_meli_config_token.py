from unittest.mock import MagicMock, patch

import requests

from odoo.tests import TransactionCase, tagged


def _mock_response(status_code, json_data):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    return response


@tagged('post_install', '-at_install')
class TestMeliConfigToken(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A dedicated test company, never `env.company` — avoids colliding
        # with a real meli.config already in this shared database.
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli token)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
        })

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_exchange_code_success(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            'access_token': 'ACCESS-1',
            'refresh_token': 'REFRESH-1',
            'expires_in': 21600,
            'user_id': 123456,
        })
        self.config._exchange_code_for_token('CODE-1', 'VERIFIER-1')
        self.assertEqual(self.config.state, 'connected')
        self.assertEqual(self.config.access_token, 'ACCESS-1')
        self.assertEqual(self.config.refresh_token, 'REFRESH-1')
        self.assertEqual(self.config.ml_user_id, '123456')
        self.assertTrue(self.config.last_connected_at)
        self.assertFalse(self.config.last_error)

        sent_payload = mock_post.call_args.kwargs['data']
        self.assertEqual(sent_payload['grant_type'], 'authorization_code')
        self.assertEqual(sent_payload['client_id'], 'test-client-id')
        self.assertEqual(sent_payload['client_secret'], 'test-client-secret')
        self.assertEqual(sent_payload['code'], 'CODE-1')
        self.assertEqual(sent_payload['code_verifier'], 'VERIFIER-1')

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_exchange_code_http_error(self, mock_post):
        mock_post.return_value = _mock_response(400, {
            'error': 'invalid_grant', 'message': 'invalid code',
        })
        self.config._exchange_code_for_token('BAD-CODE', 'VERIFIER-1')
        self.assertEqual(self.config.state, 'error')
        self.assertIn('400', self.config.last_error)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_exchange_code_connection_error(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError('boom')
        self.config._exchange_code_for_token('CODE-1', 'VERIFIER-1')
        self.assertEqual(self.config.state, 'error')
        self.assertIn('Connection error', self.config.last_error)

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_refresh_token_rotates_value(self, mock_post):
        mock_post.return_value = _mock_response(200, {
            'access_token': 'ACCESS-OLD',
            'refresh_token': 'REFRESH-OLD',
            'expires_in': 21600,
            'user_id': 123456,
        })
        self.config._exchange_code_for_token('CODE-1', 'VERIFIER-1')

        mock_post.return_value = _mock_response(200, {
            'access_token': 'ACCESS-NEW',
            'refresh_token': 'REFRESH-NEW',
            'expires_in': 21600,
            'user_id': 123456,
        })
        self.config._refresh_token()

        self.assertEqual(self.config.access_token, 'ACCESS-NEW')
        self.assertEqual(self.config.refresh_token, 'REFRESH-NEW')
        self.assertNotEqual(self.config.refresh_token, 'REFRESH-OLD')
        self.assertTrue(self.config.last_refreshed_at)

        sent_payload = mock_post.call_args.kwargs['data']
        self.assertEqual(sent_payload['grant_type'], 'refresh_token')
        self.assertEqual(sent_payload['refresh_token'], 'REFRESH-OLD')

    @patch('odoo.addons.xe_meli_connector.models.meli_config.requests.post')
    def test_malformed_200_response_preserves_tokens(self, mock_post):
        # First, set up valid tokens
        mock_post.return_value = _mock_response(200, {
            'access_token': 'ACCESS-GOOD',
            'refresh_token': 'REFRESH-GOOD',
            'expires_in': 21600,
            'user_id': 123456,
        })
        self.config._exchange_code_for_token('CODE-1', 'VERIFIER-1')
        self.assertEqual(self.config.access_token, 'ACCESS-GOOD')
        self.assertEqual(self.config.refresh_token, 'REFRESH-GOOD')

        # Now send a 200 response missing refresh_token
        mock_post.return_value = _mock_response(200, {
            'access_token': 'ACCESS-NEW',
            'expires_in': 21600,
            'user_id': 123456,
        })
        self.config._refresh_token()

        # Tokens should be unchanged and state should be error
        self.assertEqual(self.config.state, 'error')
        self.assertEqual(self.config.access_token, 'ACCESS-GOOD')
        self.assertEqual(self.config.refresh_token, 'REFRESH-GOOD')
        self.assertIn('Malformed token response', self.config.last_error)
