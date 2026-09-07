from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.tests import TransactionCase, tagged

from ..models.meli_invoice_document import MELI_INVOICE_TRANSACTION_TYPES


@tagged('post_install', '-at_install')
class TestMeliInvoiceDocument(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice document)'})
        cls.partner = cls.env['res.partner'].create({'name': 'Meli Invoice Test Buyer'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'invoice-client', 'client_secret': 'invoice-secret',
            'state': 'connected', 'ml_user_id': '999',
            'partner_id': cls.partner.id,
        })
        cls.warehouse = cls.env['stock.warehouse'].create({
            'name': 'Almacén Invoice Test', 'code': 'INVT',
            'company_id': cls.test_company.id,
        })

    def _order_by_meli_order_id(self, meli_order_id):
        return self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'client_order_ref': meli_order_id, 'meli_order_id': meli_order_id,
        })

    def _order_legacy_ventiapp(self, client_order_ref):
        # Ventiapp-created orders never set meli_order_id — only
        # client_order_ref (sometimes mirrored onto reference too).
        return self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'client_order_ref': client_order_ref, 'reference': client_order_ref,
        })

    def _http_error(self, status_code):
        response = MagicMock()
        response.status_code = status_code
        return requests.exceptions.HTTPError(response=response)

    # -- _meli_import_invoice_document_for_order (the confirmed-safe, order-driven path) --

    def test_order_path_creates_factura_for_sale_transaction_type(self):
        order = self._order_by_meli_order_id('7000000000000001')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000001', 'sale',
            )

        self.assertTrue(document)
        self.assertEqual(document.document_type, 'factura')
        self.assertEqual(document.sale_order_id, order)
        self.assertTrue(document.xml_file)

    def test_order_path_creates_nota_de_credito_for_devolution(self):
        self._order_by_meli_order_id('7000000000000002')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000002', 'devolution',
            )

        self.assertEqual(document.document_type, 'nota_de_credito')

    def test_order_path_404_returns_empty_recordset_without_raising(self):
        with patch.object(
            type(self.config), '_api_get_raw', side_effect=self._http_error(404),
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000003', 'resale',
            )

        self.assertFalse(document)

    def test_order_path_non_404_error_propagates(self):
        with patch.object(
            type(self.config), '_api_get_raw', side_effect=self._http_error(500),
        ):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                    self.test_company.id, '7000000000000004', 'sale',
                )

    def test_order_path_matches_legacy_ventiapp_order_by_client_order_ref(self):
        order = self._order_legacy_ventiapp('7000000000000005')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000005', 'sale',
            )

        self.assertEqual(document.sale_order_id, order)

    def test_meli_pack_id_mirrors_the_linked_order_even_when_matched_by_order_id(self):
        self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'client_order_ref': '2000014849956519',
            'meli_order_id': '7000000000000030', 'meli_pack_id': '2000014849956519',
        })
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000030', 'resale',
            )

        self.assertEqual(document.meli_pack_id, '2000014849956519')

    def test_order_path_falls_back_to_pack_id_when_no_order_matches(self):
        # A resale pack invoice: Mercado Libre bills the whole cart as one
        # document, so this fallback exists in case a document's own
        # meli_order_id ever ends up being a pack id rather than one
        # specific sibling order's id.
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'meli_pack_id': '2000014849956519',
        })
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '2000014849956519', 'resale',
            )

        self.assertEqual(document.sale_order_id, order)
        self.assertEqual(document.meli_pack_id, '2000014849956519')

    def test_order_path_upserts_instead_of_duplicating(self):
        self._order_by_meli_order_id('7000000000000006')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<v1/>',
        ):
            first = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000006', 'sale',
            )
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<v2/>',
        ):
            second = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000006', 'sale',
            )

        self.assertEqual(first.id, second.id)
        documents = self.env['meli.invoice.document'].search([
            ('meli_order_id', '=', '7000000000000006'), ('transaction_type', '=', 'sale'),
        ])
        self.assertEqual(len(documents), 1)

    # -- issue_date (parsed straight from the CFDI's own Fecha attribute) --

    def test_order_path_parses_issue_date_from_cfdi_fecha_as_monterrey_local_time(self):
        self._order_by_meli_order_id('7000000000000020')
        with patch.object(
            type(self.config), '_api_get_raw',
            return_value=(
                b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
                b'Fecha="2026-09-04T15:30:00"/>'
            ),
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000020', 'sale',
            )

        # America/Monterrey is UTC-6 (no DST as of 2026) -> 21:30 UTC.
        self.assertEqual(document.issue_date, fields.Datetime.to_datetime('2026-09-04 21:30:00'))

    def test_order_path_missing_fecha_attribute_leaves_issue_date_blank(self):
        self._order_by_meli_order_id('7000000000000021')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000021', 'sale',
            )

        self.assertFalse(document.issue_date)

    def test_order_path_malformed_xml_does_not_block_saving_the_document(self):
        self._order_by_meli_order_id('7000000000000022')
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'not xml at all',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000022', 'sale',
            )

        self.assertTrue(document)
        self.assertFalse(document.issue_date)
        self.assertTrue(document.xml_file)

    def test_order_path_reprocessing_does_not_blank_a_previously_parsed_issue_date(self):
        self._order_by_meli_order_id('7000000000000023')
        with patch.object(
            type(self.config), '_api_get_raw',
            return_value=(
                b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4" '
                b'Fecha="2026-09-01T09:00:00"/>'
            ),
        ):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000023', 'sale',
            )
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000023', 'sale',
            )

        self.assertEqual(document.issue_date, fields.Datetime.to_datetime('2026-09-01 15:00:00'))

    # -- _meli_import_invoice_document (the webhook path, metadata schema unconfirmed) --

    def test_webhook_path_stores_document_using_order_id_from_metadata(self):
        self._order_by_meli_order_id('7000000000000007')
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'order_id': '7000000000000007', 'transaction_type': 'sale',
                'status': 'authorized',
            },
        ), patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000001',
            )

        self.assertEqual(document.meli_invoice_id, '8000000001')
        self.assertEqual(document.meli_order_id, '7000000000000007')
        self.assertEqual(document.document_type, 'factura')
        self.assertEqual(document.status, 'authorized')

    def test_webhook_path_reads_transaction_type_from_nested_fiscal_data(self):
        self._order_by_meli_order_id('7000000000000008')
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'order_id': '7000000000000008',
                'fiscal_data': {'transaction_type': 'devolution'},
            },
        ), patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000002',
            )

        self.assertEqual(document.transaction_type, 'devolution')
        self.assertEqual(document.document_type, 'nota_de_credito')

    def test_webhook_path_metadata_failure_still_stores_the_xml(self):
        # The XML fetch only needs invoice_id (always reliable) — a
        # metadata lookup failure must not lose the document itself.
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.RequestException("ML is down"),
        ), patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000003',
            )

        self.assertTrue(document)
        self.assertFalse(document.meli_order_id)
        self.assertFalse(document.sale_order_id)
        self.assertTrue(document.xml_file)

    def test_webhook_path_reads_order_id_from_items_external_order_id(self):
        # Confirmed against a real 'invoices' notification (2026-09-04,
        # a meli_resale invoice): there is no top-level order_id field
        # at all — it's items[0].external_order_id.
        self._order_by_meli_order_id('7000000000000009')
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7000000000000009'}],
                'fiscal_data': {'transaction_type': 'resale'},
            },
        ), patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ) as mocked_raw:
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000004',
            )

        self.assertEqual(document.meli_order_id, '7000000000000009')
        self.assertEqual(document.transaction_type, 'resale')
        # Prefers the order-based endpoint over invoice_id-based, since
        # order_id was successfully determined.
        called_path = mocked_raw.call_args.args[0]
        self.assertIn('/order/7000000000000009/xml', called_path)

    def test_webhook_path_falls_back_to_invoice_id_when_order_fetch_404s(self):
        # Real-world case (2026-09-04): a meli_resale invoice's order-
        # based fetch 404'd. Must still try the invoice_id-based
        # endpoint rather than giving up.
        self._order_by_meli_order_id('7000000000000010')
        response_404 = MagicMock()
        response_404.status_code = 404

        def fake_get_raw(path, params=None, headers=None):
            if '/order/' in path:
                raise requests.exceptions.HTTPError(response=response_404)
            return b'<cfdi:Comprobante/>'

        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7000000000000010'}],
                'fiscal_data': {'transaction_type': 'resale'},
            },
        ), patch.object(type(self.config), '_api_get_raw', side_effect=fake_get_raw):
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000005',
            )

        self.assertTrue(document.xml_file)

    def test_webhook_path_stores_record_without_file_when_both_fetches_404(self):
        response_404 = MagicMock()
        response_404.status_code = 404

        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7000000000000011'}],
                'fiscal_data': {'transaction_type': 'resale'},
            },
        ), patch.object(
            type(self.config), '_api_get_raw',
            side_effect=requests.exceptions.HTTPError(response=response_404),
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '8000000006',
            )

        self.assertTrue(document)
        self.assertFalse(document.xml_file)
        self.assertEqual(document.meli_order_id, '7000000000000011')

    # -- _meli_upsert replacement handling (cancelled + reissued documents) --

    def test_webhook_path_creates_a_new_record_when_invoice_id_differs_for_same_order_and_type(self):
        # Confirmed with Mercado Libre support (2026-09-08, case
        # 480258955): a generic-RFC "factura global" can be cancelled
        # and replaced by a new invoice stamped to the buyer's real
        # RFC — same order, same transaction_type ('sale'), but a
        # DIFFERENT invoice_id. Both must survive as separate records.
        self._order_by_meli_order_id('7100000000000001')
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7100000000000001'}],
                'fiscal_data': {'transaction_type': 'sale'},
                'status': 'cancelled',
            },
        ), patch.object(type(self.config), '_api_get_raw', return_value=b'<global/>'):
            original = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '9100000000000001',
            )
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7100000000000001'}],
                'fiscal_data': {'transaction_type': 'sale'},
                'status': 'authorized',
            },
        ), patch.object(type(self.config), '_api_get_raw', return_value=b'<reissued/>'):
            replacement = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '9100000000000002',
            )

        self.assertNotEqual(original.id, replacement.id)
        self.assertEqual(original.meli_invoice_id, '9100000000000001')
        self.assertEqual(original.status, 'cancelled')
        self.assertEqual(replacement.meli_invoice_id, '9100000000000002')
        self.assertEqual(replacement.status, 'authorized')
        documents = self.env['meli.invoice.document'].search([
            ('meli_order_id', '=', '7100000000000001'), ('transaction_type', '=', 'sale'),
        ])
        self.assertEqual(len(documents), 2)

    def test_webhook_path_adopts_a_blank_invoice_id_record_instead_of_duplicating(self):
        # The order-based recovery path never learns invoice_id. If the
        # webhook later delivers the SAME document with its real
        # invoice_id, it must fill in that same record, not duplicate it.
        self._order_by_meli_order_id('7100000000000002')
        with patch.object(type(self.config), '_api_get_raw', return_value=b'<v1/>'):
            recovered = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7100000000000002', 'sale',
            )
        self.assertFalse(recovered.meli_invoice_id)

        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7100000000000002'}],
                'fiscal_data': {'transaction_type': 'sale'},
            },
        ), patch.object(type(self.config), '_api_get_raw', return_value=b'<v2/>'):
            enriched = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '9100000000000003',
            )

        self.assertEqual(recovered.id, enriched.id)
        self.assertEqual(enriched.meli_invoice_id, '9100000000000003')
        documents = self.env['meli.invoice.document'].search([
            ('meli_order_id', '=', '7100000000000002'), ('transaction_type', '=', 'sale'),
        ])
        self.assertEqual(len(documents), 1)

    def test_webhook_path_reprocessing_same_invoice_id_updates_not_duplicates(self):
        # A missed_feeds reconciliation or a manual status refresh can
        # legitimately re-process the exact same invoice_id — must
        # always update that one record, never create a second.
        self._order_by_meli_order_id('7100000000000003')
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7100000000000003'}],
                'fiscal_data': {'transaction_type': 'sale'}, 'status': 'authorized',
            },
        ), patch.object(type(self.config), '_api_get_raw', return_value=b'<v1/>'):
            first = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '9100000000000004',
            )
        with patch.object(
            type(self.config), '_api_get',
            return_value={
                'items': [{'external_order_id': '7100000000000003'}],
                'fiscal_data': {'transaction_type': 'sale'}, 'status': 'cancelled',
            },
        ), patch.object(type(self.config), '_api_get_raw', return_value=b'<v1/>'):
            second = self.env['meli.invoice.document']._meli_import_invoice_document(
                self.test_company.id, '9100000000000004',
            )

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.status, 'cancelled')
        documents = self.env['meli.invoice.document'].search([
            ('meli_invoice_id', '=', '9100000000000004'),
        ])
        self.assertEqual(len(documents), 1)

    # -- _meli_recover_invoices_in_range (the recovery wizard's single job) --

    def _order_in_range(self, meli_order_id, date_order):
        return self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse.id,
            'client_order_ref': meli_order_id, 'meli_order_id': meli_order_id,
            'date_order': date_order,
        })

    def _recovery_job_identity_keys(self):
        jobs = self.env['queue.job'].sudo().search([
            ('model_name', '=', 'meli.invoice.document'),
            ('method_name', '=', '_meli_import_invoice_document_for_order'),
        ])
        return set(jobs.mapped('identity_key'))

    def test_recover_in_range_enqueues_one_job_per_order_per_transaction_type(self):
        # No HTTP call happens inside this method either — same
        # enumerate-and-enqueue-only contract as
        # _meli_import_invoice_document_for_order's caller. This is the
        # method the recovery wizard's single job calls, so the actual
        # per-order/per-transaction_type fan-out happens here, in the
        # background — never inside the wizard's own request.
        self._order_in_range('9200000000000001', fields.Datetime.now())

        with patch.object(type(self.config), '_api_get_raw') as mocked_raw:
            queued = self.env['meli.invoice.document']._meli_recover_invoices_in_range(
                self.test_company.id,
                fields.Datetime.subtract(fields.Datetime.now(), hours=1),
                fields.Datetime.now(),
            )

        mocked_raw.assert_not_called()
        self.assertEqual(queued, len(MELI_INVOICE_TRANSACTION_TYPES))
        keys = self._recovery_job_identity_keys()
        for transaction_type in MELI_INVOICE_TRANSACTION_TYPES:
            self.assertIn(
                f'meli_import_invoice_order_9200000000000001_{transaction_type}', keys,
            )

    def test_recover_in_range_ignores_orders_outside_the_range(self):
        self._order_in_range(
            '9200000000000002',
            fields.Datetime.subtract(fields.Datetime.now(), days=10),
        )

        queued = self.env['meli.invoice.document']._meli_recover_invoices_in_range(
            self.test_company.id,
            fields.Datetime.subtract(fields.Datetime.now(), hours=1),
            fields.Datetime.now(),
        )

        self.assertEqual(queued, 0)
        keys = self._recovery_job_identity_keys()
        self.assertFalse(any('9200000000000002' in key for key in keys))

    def test_recover_in_range_without_active_connection_is_a_noop(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli connection, invoice recovery)'})

        queued = self.env['meli.invoice.document']._meli_recover_invoices_in_range(
            other_company.id,
            fields.Datetime.subtract(fields.Datetime.now(), hours=1),
            fields.Datetime.now(),
        )

        self.assertEqual(queued, 0)

    # -- action_meli_refresh_status (manual, on-demand status pull) --

    def _document_with_invoice_id(self, meli_invoice_id, order_id):
        # company_id is related to sale_order_id.company_id — a matching
        # order is needed so the config lookup inside
        # action_meli_refresh_status can find self.config.
        self._order_by_meli_order_id(order_id)
        return self.env['meli.invoice.document'].create({
            'meli_order_id': order_id, 'meli_invoice_id': meli_invoice_id,
            'transaction_type': 'sale',
        })

    def _document_without_invoice_id(self, order_id):
        self._order_by_meli_order_id(order_id)
        return self.env['meli.invoice.document'].create({
            'meli_order_id': order_id, 'transaction_type': 'sale',
        })

    def test_refresh_status_updates_documents_with_a_known_invoice_id(self):
        document = self._document_with_invoice_id('9300000000000001', '9300000000000001')
        with patch.object(
            type(self.config), '_api_get', return_value={'status': 'cancelled'},
        ):
            result = document.action_meli_refresh_status()

        self.assertEqual(document.status, 'cancelled')
        self.assertIn('1 documento(s) actualizado(s)', result['params']['message'])

    def test_refresh_status_skips_documents_without_invoice_id(self):
        document = self._document_without_invoice_id('9300000000000002')
        with patch.object(type(self.config), '_api_get') as mocked_get:
            result = document.action_meli_refresh_status()

        mocked_get.assert_not_called()
        self.assertFalse(document.status)
        self.assertIn('omitido', result['params']['message'])

    def test_refresh_status_api_failure_is_skipped_not_raised(self):
        document = self._document_with_invoice_id('9300000000000003', '9300000000000003')
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.RequestException("ML is down"),
        ):
            result = document.action_meli_refresh_status()

        self.assertFalse(document.status)
        self.assertIn('0 documento(s) actualizado(s)', result['params']['message'])

    def test_refresh_status_handles_mixed_selection(self):
        with_id = self._document_with_invoice_id('9300000000000004', '9300000000000004')
        without_id = self._document_without_invoice_id('9300000000000005')
        with patch.object(
            type(self.config), '_api_get', return_value={'status': 'authorized'},
        ):
            result = (with_id | without_id).action_meli_refresh_status()

        self.assertEqual(with_id.status, 'authorized')
        self.assertFalse(without_id.status)
        self.assertIn('1 documento(s) actualizado(s)', result['params']['message'])
        self.assertIn('1 omitido(s)', result['params']['message'])
