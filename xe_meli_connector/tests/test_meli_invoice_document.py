from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.exception import RetryableJobError

from ..models.meli_invoice_document import MELI_INVOICE_TRANSACTION_TYPES


@tagged('post_install', '-at_install')
class TestMeliInvoiceDocument(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice document)'})
        # This class was originally built to test document
        # matching/creation logic only — no test here ever confirmed a
        # sale order or posted an invoice, so no chart of accounts was
        # ever loaded. Task 3's new
        # test_upsert_triggers_the_reconciler_when_already_delivered
        # below is the first one to run a real
        # action_confirm/button_validate/_create_invoices flow, which
        # needs a sale journal to exist. 'generic_coa' (same choice as
        # TestMeliFullCancellationAutomation in
        # test_meli_full_cancellation.py) is the base, non-Mexican chart
        # — enough to let an invoice get created and posted; this test
        # only needs to prove the reconciler fires, not exercise CFDI-
        # blocking logic, so there's no need for the heavier 'mx' chart
        # that class TestMeliInvoicingLifecycle uses instead.
        cls.env['account.chart.template'].try_loading(
            'generic_coa', company=cls.test_company, install_demo=False,
        )
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
        cls.product = cls.env['product.product'].create({
            'name': 'Producto Invoice Document Test', 'type': 'product',
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

    def test_order_path_second_pack_sibling_reuses_the_same_document(self):
        first_order = self._order_by_meli_order_id('7000000000000100')
        first_order.meli_pack_id = '8000000000000001'
        second_order = self._order_by_meli_order_id('7000000000000101')
        second_order.meli_pack_id = '8000000000000001'

        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            first_document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000100', 'sale',
            )
            second_document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000101', 'sale',
            )

        self.assertEqual(first_document, second_document)
        self.assertEqual(
            self.env['meli.invoice.document'].search_count([
                ('transaction_type', '=', 'sale'),
                ('sale_order_id', 'in', (first_order | second_order).ids),
            ]),
            1,
        )

    def test_order_path_devolution_does_not_reuse_a_pack_siblings_own_credit_note(self):
        """Final review Fix 4: the pack-based document reuse/fan-out
        exercised by test_order_path_second_pack_sibling_reuses_the_same_document
        above is correct for FACTURAS — Mercado Libre genuinely invoices
        a whole pack with ONE physical CFDI — but WRONG for DEVOLUCIONES
        (credit notes): each sibling gets its OWN, independent
        devolución, never shared per-pack. Before this fix, importing
        sibling B's own devolución found sibling A's devolución row
        (matched purely via the shared meli_pack_id/sale_order_id) and
        silently overwrote it with B's own meli_order_id and XML,
        destroying A's own credit-note record.
        """
        first_order = self._order_by_meli_order_id('7000000000000102')
        first_order.meli_pack_id = '8000000000000002'
        second_order = self._order_by_meli_order_id('7000000000000103')
        second_order.meli_pack_id = '8000000000000002'

        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            first_document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000102', 'devolution',
            )
            second_document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000103', 'devolution',
            )

        self.assertNotEqual(
            first_document, second_document,
            "each sibling's own devolución must end up as its own, distinct document row",
        )
        self.assertEqual(first_document.meli_order_id, '7000000000000102')
        self.assertEqual(first_document.sale_order_id, first_order)
        self.assertEqual(second_document.meli_order_id, '7000000000000103')
        self.assertEqual(second_document.sale_order_id, second_order)
        self.assertEqual(
            self.env['meli.invoice.document'].search_count([
                ('transaction_type', '=', 'devolution'),
                ('sale_order_id', 'in', (first_order | second_order).ids),
            ]),
            2,
        )

    def test_order_path_resolves_second_pack_sibling_added_via_task2_consolidation(self):
        """The real Task-2-consolidated shape (distinct from
        test_order_path_second_pack_sibling_reuses_the_same_document
        above, which uses two separate, legacy-style sale.order rows,
        each with its own meli_order_id).

        Since Task 2, importing a second Mercado Libre order for a pack
        already known here does NOT create a second sale.order — it
        merges the second sibling's own line into the FIRST sibling's
        sale.order instead (sale.order._meli_add_pack_sibling_lines).
        So the ONE consolidated sale.order's own meli_order_id is only
        ever the FIRST sibling's id; the SECOND sibling's own order id
        lives only on the sale.order.line it added
        (sale.order.line.meli_order_id).

        Before the C1 fix, neither
        sale.order._meli_find_order_by_id_or_pack nor this model's own
        _compute_sale_order_id checked sale.order.line.meli_order_id —
        so an invoice-document notification reporting the SECOND
        sibling's own order id (not the pack id, not the first
        sibling's id) resolved to nothing at all (sale_order_id blank),
        and a later import for the other sibling's id would have
        created a second, duplicate document instead of finding this
        one.
        """
        self.config.warehouse_default_id = self.warehouse
        first_product = self.env['product.product'].create({
            'name': 'Meli Invoice Test Pack Sibling Product 1',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': first_product.id, 'meli_sku': 'ZTEST-INVDOC-PACK1',
        })
        second_product = self.env['product.product'].create({
            'name': 'Meli Invoice Test Pack Sibling Product 2',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': 'ZTEST-INVDOC-PACK2',
        })

        def _order_data(order_id, sku, pack_id):
            return {
                'id': order_id, 'status': 'paid', 'pack_id': pack_id,
                'shipping': {}, 'date_created': None, 'date_closed': None,
                'order_items': [{
                    'item': {'id': 'MLMX', 'seller_sku': sku},
                    'quantity': 1, 'unit_price': 100.0,
                }],
            }

        SaleOrder = self.env['sale.order']
        first_order = SaleOrder._meli_create_from_order_data(
            self.config,
            _order_data('7400000000000001', 'ZTEST-INVDOC-PACK1', '8400000000000010'),
        )
        second_order = SaleOrder._meli_create_from_order_data(
            self.config,
            _order_data('7400000000000002', 'ZTEST-INVDOC-PACK2', '8400000000000010'),
        )
        # Sanity check that this really is the Task 2 consolidated
        # shape, not two separate sale orders.
        self.assertEqual(first_order, second_order)
        self.assertEqual(first_order.meli_order_id, '7400000000000001')
        self.assertEqual(
            sorted(first_order.order_line.mapped('meli_order_id')),
            ['7400000000000001', '7400000000000002'],
        )

        # The invoice-document metadata reports the SECOND sibling's
        # own order id — never seen anywhere on the sale.order itself.
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7400000000000002', 'sale',
            )

        self.assertEqual(document.sale_order_id, first_order)
        self.assertEqual(document.company_id, first_order.company_id)
        self.assertEqual(document.meli_pack_id, '8400000000000010')
        self.assertEqual(
            self.env['meli.invoice.document'].search_count([
                ('transaction_type', '=', 'sale'),
                ('sale_order_id', '=', first_order.id),
            ]),
            1,
        )

        # A later notification for the FIRST sibling's own order id
        # must reuse this same document, not create a second one.
        with patch.object(
            type(self.config), '_api_get_raw', return_value=b'<cfdi:Comprobante/>',
        ):
            second_document = self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7400000000000001', 'sale',
            )

        self.assertEqual(document, second_document)
        self.assertEqual(
            self.env['meli.invoice.document'].search_count([
                ('transaction_type', '=', 'sale'),
                ('sale_order_id', '=', first_order.id),
            ]),
            1,
        )

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

    # -- account.move.meli_invoice_document_id --

    def test_account_move_has_meli_invoice_document_field(self):
        move = self.env['account.move'].new({'move_type': 'out_invoice'})
        self.assertIn('meli_invoice_document_id', move._fields)
        self.assertEqual(
            move._fields['meli_invoice_document_id'].comodel_name,
            'meli.invoice.document',
        )

    # -- Task 3: _meli_upsert wires into sale.order._meli_reconcile_invoicing --

    def test_upsert_triggers_the_reconciler_when_already_delivered(self):
        # _order_by_meli_order_id builds a bare order with no lines —
        # every other caller in this file only needs order resolution,
        # never a real delivery. This is the first test in this file to
        # actually run the order through confirm/deliver, so it needs a
        # product line and available stock on top of what the helper
        # gives it (same order of operations as
        # TestMeliFullCancellationAutomation._create_full_order in
        # test_meli_full_cancellation.py: stock made available before
        # the order is confirmed).
        self.env['stock.quant']._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, 10,
        )
        order = self._order_by_meli_order_id('7000000000000200')
        order.write({'order_line': [(0, 0, {
            'product_id': self.product.id, 'product_uom_qty': 1,
        })]})
        order.action_confirm()
        order.picking_ids.button_validate()
        self.assertFalse(order.invoice_ids)

        with patch.object(
            type(self.config), '_api_get_raw',
            # A bare '<cfdi:Comprobante/>' (used by every other test in
            # this file, which never exercises the real reconciler) is
            # not well-formed XML on its own — lxml raises "Namespace
            # prefix cfdi on Comprobante is not defined" (confirmed in
            # practice). This test is the first one whose upsert actually
            # reaches _meli_relate_invoice_document ->
            # _l10n_mx_edi_cfdi_invoice_document_sent, which parses the
            # XML for real, so it needs the namespace declared. That's
            # all it needs, though: l10n_mx_edi's own
            # _create_update_document only parses/pretty-prints the XML
            # and stores it as an attachment — it never requires a
            # Complemento/TimbreFiscalDigital node to succeed (that node
            # only affects whether l10n_mx_edi_cfdi_uuid ends up
            # populated, see _fake_cfdi_xml's docstring in
            # test_meli_full_cancellation.py), which this test doesn't
            # assert on.
            return_value=b'<cfdi:Comprobante xmlns:cfdi="http://www.sat.gob.mx/cfd/4"/>',
        ):
            self.env['meli.invoice.document']._meli_import_invoice_document_for_order(
                self.test_company.id, '7000000000000200', 'sale',
            )

        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(invoice), 1)

    # -- Task 4: _meli_upsert recovers a missing order via _meli_import_order --

    def test_upsert_recovers_missing_order_via_meli_import_order(self):
        """Trigger B (2026-09-09, cancelled-order-recovery plan): a
        credit-note document arrives for an order this connector has
        never created — _meli_upsert must now ask _meli_import_order to
        fetch it fresh from Mercado Libre instead of silently leaving
        sale_order_id False forever.
        """
        fake_order_data = {
            'id': 'MID-RECOVERY-0001', 'status': 'cancelled', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [],
        }
        with patch.object(
            type(self.config), '_api_get', return_value=fake_order_data,
        ) as mocked_api_get, patch.object(
            type(self.env['sale.order']), '_meli_create_from_order_data',
        ) as mocked_create:
            mocked_create.return_value = self.env['sale.order'].browse()
            document = self.env['meli.invoice.document']._meli_upsert(
                'MID-RECOVERY-0001', 'devolution', b'<fake/>',
                meli_invoice_id='9200000000000001',
                company_id=self.test_company.id,
            )

        mocked_api_get.assert_called_once_with('/orders/MID-RECOVERY-0001')
        mocked_create.assert_called_once()
        self.assertEqual(document.meli_order_id, 'MID-RECOVERY-0001')

    def test_upsert_propagates_retryable_job_error_from_recovery(self):
        """Final whole-branch review (2026-09-09), Important I1: a
        transient Mercado Libre API failure (429/5xx/network blip) while
        attempting Trigger B's recovery must reach queue_job's own retry
        machinery, not get silently logged and swallowed as a permanent
        failure — same guard already applied to
        _meli_import_order_for_batch_line (sale_order.py) and
        _meli_import_invoice_document_for_batch_line (this module).

        Deliberately does NOT exercise the removed
        `with self.env.cr.savepoint():` around this call (final
        whole-branch review, Critical C1) — that would require forcing
        l10n_mx_edi.document._can_commit() to return True inside a
        TransactionCase test, risking a real cr.commit() against the
        shared test database. Verified instead by reading the code: no
        `with self.env.cr.savepoint():` remains around the
        _meli_import_order call in meli_invoice_document.py.
        """
        with patch.object(
            type(self.env['sale.order']), '_meli_import_order',
            side_effect=RetryableJobError('transient'),
        ):
            with self.assertRaises(RetryableJobError):
                self.env['meli.invoice.document']._meli_upsert(
                    'MID-RECOVERY-RETRY-0001', 'sale', b'<fake/>',
                    meli_invoice_id='9200000000000099',
                    company_id=self.test_company.id,
                )

    def test_upsert_does_not_recover_when_order_already_resolves(self):
        """No wasted recovery attempt when the document already resolves
        to a real, existing sale order — the ordinary, common case."""
        order = self._order_by_meli_order_id('MID-RECOVERY-0002')
        with patch.object(type(self.config), '_api_get') as mocked_api_get:
            document = self.env['meli.invoice.document']._meli_upsert(
                'MID-RECOVERY-0002', 'sale', b'<fake/>',
                meli_invoice_id='9200000000000002',
                company_id=self.test_company.id,
            )

        mocked_api_get.assert_not_called()
        self.assertEqual(document.sale_order_id, order)

    def test_upsert_orphaned_document_recovers_full_order_end_to_end(self):
        """Genuinely end-to-end: no mocking of _meli_create_from_order_data
        — proves Trigger B really does close the loop all the way through
        Tasks 2-3's own recovery logic, not just that _meli_import_order
        gets called. Self-contained fixture (Mexican coa + fulfillment
        warehouse): this file's shared setUpClass deliberately stays
        lightweight (generic_coa, no fulfillment warehouse) for its many
        other, simpler tests.
        """
        company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli invoice document e2e recovery)'})
        self.env['account.chart.template'].try_loading(
            'mx', company=company, install_demo=False,
        )
        partner = self.env['res.partner'].create({'name': 'Meli E2E Recovery Buyer'})
        warehouse_fulfillment = self.env['stock.warehouse'].create({
            'name': 'Almacen E2E Recovery', 'code': 'E2ER',
            'company_id': company.id,
        })
        product = self.env['product.product'].create({
            'name': 'Producto E2E Recovery', 'type': 'product',
            'company_id': company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': product.id, 'meli_sku': 'ZTEST-E2E-RECOVERY',
        })
        self.env['stock.quant']._update_available_quantity(
            product, warehouse_fulfillment.lot_stock_id, 10,
        )
        config = self.env['meli.config'].create({
            'company_id': company.id,
            'client_id': 'e2e-recovery-client', 'client_secret': 'e2e-recovery-secret',
            'state': 'connected', 'partner_id': partner.id,
            'warehouse_fulfillment_id': warehouse_fulfillment.id,
        })
        fake_order_data = {
            'id': 'MID-E2E-0001', 'status': 'cancelled', 'pack_id': False,
            'shipping': {}, 'date_created': None, 'date_closed': None,
            'order_items': [{
                'item': {'id': 'MLM-E2E', 'seller_sku': 'ZTEST-E2E-RECOVERY'},
                'quantity': 1, 'unit_price': 75.0,
            }],
        }

        with patch.object(
            type(config), '_api_get', return_value=fake_order_data,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['account.move']),
            '_l10n_mx_edi_cfdi_invoice_try_send',
            side_effect=AssertionError("must never be called — Mercado Libre owns the CFDI"),
        ):
            document = self.env['meli.invoice.document']._meli_upsert(
                'MID-E2E-0001', 'sale', b'<fake/>',
                meli_invoice_id='9200000000000099', company_id=company.id,
            )

        order = self.env['sale.order'].search([('meli_order_id', '=', 'MID-E2E-0001')])
        self.assertTrue(order, "the order should have been recovered end to end")
        self.assertEqual(order.state, 'cancel')
        self.assertTrue(order.meli_auto_cancellation_processed)
        self.assertEqual(document.sale_order_id, order)
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.meli_invoice_document_id, document)
