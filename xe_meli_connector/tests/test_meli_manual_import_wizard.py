from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliManualImportWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli manual import)'})
        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Test',
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacén Default Manual Import Test', 'code': 'DEFTM',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'manual-import-client', 'client_secret': 'manual-import-secret',
            'state': 'connected', 'access_token': 'test-access-token',
            'refresh_token': 'test-refresh-token', 'partner_id': cls.partner.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def _wizard(self, order_id):
        return self.env['meli.manual.import.wizard'].with_company(
            self.test_company
        ).create({'order_id': order_id})

    def test_shipment_fetch_failure_raises_a_friendly_user_error(self):
        # Fix 2026-09-10: _meli_import_order now raises RetryableJobError
        # (via _meli_fetch_shipment_records) when a shipping-details fetch
        # fails, instead of silently creating the order without them. This
        # wizard has no queue_job to retry it automatically — it must turn
        # that into a plain, actionable message instead of a raw traceback.
        order_data = {
            'id': '2000018198399001', 'status': 'paid', 'pack_id': False,
            'shipping': {'id': 1}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-MANUAL1', 'seller_sku': 'ZTEST-MANUAL1'},
                'quantity': 1, 'unit_price': 50.0,
            }],
        }
        wizard = self._wizard('2000018198399001')
        with patch.object(
            type(self.config), '_api_get',
            side_effect=[order_data, requests.exceptions.RequestException('boom')],
        ):
            with self.assertRaises(UserError) as err_ctx:
                wizard.action_import()

        self.assertNotIn('RetryableJobError', str(err_ctx.exception))
