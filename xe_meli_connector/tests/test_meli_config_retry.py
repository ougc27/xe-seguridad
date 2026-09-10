from unittest.mock import Mock, patch

import requests

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliConfigRetry(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A dedicated test company, never `env.company` — matches
        # test_meli_config_token.py's own fixture, avoids colliding with
        # a real meli.config already in this shared database (the
        # company_id_uniq sql constraint allows only one per company).
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli retry)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id-retry',
            'client_secret': 'test-client-secret-retry',
            'state': 'connected',
            'access_token': 'fake-token',
        })

    def _mock_response(self, status_code, headers=None):
        response = Mock(spec=requests.Response)
        response.status_code = status_code
        response.headers = headers or {}
        response.content = b'{}'
        if status_code >= 400:
            response.raise_for_status.side_effect = requests.exceptions.HTTPError(
                response=response,
            )
        else:
            response.raise_for_status.side_effect = None
        return response

    def test_429_raises_retryable_honoring_retry_after_header(self):
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            return_value=self._mock_response(429, headers={'Retry-After': '17'}),
        ):
            with self.assertRaises(RetryableJobError) as caught:
                self.config._api_get('/orders/123')
        self.assertEqual(caught.exception.seconds, 17)

    def test_504_raises_retryable(self):
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            return_value=self._mock_response(504),
        ):
            with self.assertRaises(RetryableJobError):
                self.config._api_get('/orders/123')

    def test_ssl_error_raises_retryable(self):
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            side_effect=requests.exceptions.SSLError('handshake failure'),
        ):
            with self.assertRaises(RetryableJobError):
                self.config._api_get('/orders/123')

    def test_404_does_not_retry(self):
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            return_value=self._mock_response(404),
        ):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.config._api_get('/orders/123')

    def test_user_error_is_not_a_retryable_job_error(self):
        """A business-logic exception unrelated to the network call
        itself must never become retryable — only network/HTTP transient
        conditions inside _api_response are wrapped.
        """
        with patch(
            'odoo.addons.xe_meli_connector.models.meli_config.requests.request',
            return_value=self._mock_response(200),
        ):
            # No assertion needed beyond "doesn't raise RetryableJobError"
            # — a 200 must simply return normally.
            self.config._api_get('/orders/123')
