from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from psycopg2 import OperationalError, errorcodes


def _fake_operational_error(pgcode):
    """psycopg2.OperationalError.pgcode is a read-only C-level attribute
    on a plain instance — can't be set after construction. A small
    subclass with pgcode as a settable property is the standard way to
    build one with a specific error code for a test.
    """
    class _FakeOperationalError(OperationalError):
        def __init__(self, code):
            super().__init__('fake db error')
            self._pgcode = code

        @property
        def pgcode(self):
            return self._pgcode

    return _FakeOperationalError(pgcode)

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.exception import RetryableJobError


@tagged('post_install', '-at_install')
class TestSaleOrderMeliImport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Force English: several assertions below check for English
        # substrings in translatable message_post bodies. This database's
        # admin user has a Spanish `lang`, which would otherwise render
        # those messages via i18n/es.po and break the assertions.
        cls.env = cls.env(context=dict(cls.env.context, lang='en_US'))
        # Dedicated test company/data throughout — never touch env.company,
        # to avoid colliding with the real meli.config/sale.order data that
        # already lives in this shared database.
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli sales import)'})

        cls.product = cls.env['product.product'].create({
            'name': 'Mesa de prueba', 'company_id': cls.test_company.id,
        })
        cls.env['meli.sku.mapping'].create({
            'product_id': cls.product.id, 'meli_sku': 'ZTEST-MPTP01',
        })

        cls.partner = cls.env['res.partner'].create({
            'name': 'Mercado Libre Test',
            # Matches the real "MERCADO LIBRE" partner (id 87659): a
            # base_automation rule blocks action_confirm() unless
            # x_cop is set (custom Studio field, label "Tipo").
            'x_cop': 'cliente',
        })
        cls.salesperson = cls.env['res.users'].create({
            'name': 'Meli Sales Test User', 'login': 'meli_sales_test_user',
        })
        cls.team = cls.env['crm.team'].create({
            'name': 'MARKETPLACE Test', 'company_id': cls.test_company.id,
        })
        cls.warehouse_fulfillment = cls.env['stock.warehouse'].create({
            'name': 'Almacén Full Test', 'code': 'FULT',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_default = cls.env['stock.warehouse'].create({
            'name': 'Almacén Default Test', 'code': 'DEFT',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
            'state': 'connected',
            'access_token': 'test-access-token',
            'refresh_token': 'test-refresh-token',
            'partner_id': cls.partner.id,
            'sale_team_id': cls.team.id,
            'salesperson_id': cls.salesperson.id,
            'warehouse_fulfillment_id': cls.warehouse_fulfillment.id,
            'warehouse_default_id': cls.warehouse_default.id,
        })

    def setUp(self):
        super().setUp()
        # Fix 2026-09-19/20: every test in this class predates
        # sale.order._meli_fetch_buyer_shipping_surcharge (see that
        # method's own docstring) — several already give their order a
        # real shipping id (to exercise _meli_fetch_custom_shipping_cost
        # or _meli_fetch_logistic_type) without mocking a shipment-costs
        # response, since nothing needed one before this fix existed.
        # Neutralized here for every test except the ones that actually
        # test this new method/behavior themselves.
        exempt = {
            'test_fetch_buyer_shipping_surcharge_reads_receiver_cost',
            'test_fetch_buyer_shipping_surcharge_skips_custom_shipments',
            'test_fetch_buyer_shipping_surcharge_reraises_request_exception',
            'test_buyer_shipping_surcharge_adds_a_line_to_a_new_order',
        }
        if self._testMethodName not in exempt:
            patcher = patch.object(
                type(self.env['sale.order']),
                '_meli_fetch_buyer_shipping_surcharge',
                return_value=None,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def _order_data(self, order_id='2000014726421911', sku='ZTEST-MPTP01',
                     status='paid', shipping=None,
                     date_created=None, date_closed=None, pack_id=None,
                     buyer_id=None):
        data = {
            'id': order_id,
            'status': status,
            'pack_id': pack_id,
            'shipping': shipping or {},
            'date_created': date_created,
            'date_closed': date_closed,
            'order_items': [{
                'item': {'id': 'MLM123', 'seller_sku': sku},
                'quantity': 2,
                'unit_price': 100.0,
            }],
        }
        if buyer_id is not None:
            data['buyer'] = {'id': buyer_id}
        return data

    def test_mapped_sku_creates_and_confirms_order(self):
        order_data = self._order_data()
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.client_order_ref, '2000014726421911')
        # "reference" is Odoo's standard payment-reference field, relabeled
        # "OC Cliente" in this instance's view — must match client_order_ref.
        self.assertEqual(order.reference, '2000014726421911')
        self.assertEqual(order.meli_order_id, '2000014726421911')
        self.assertFalse(order.meli_pack_id)
        self.assertEqual(order.origin, 'XE-ML-XEBRANDS')
        self.assertEqual(order.meli_sync_source, 'xe_meli_connector')
        self.assertEqual(order.partner_id, self.partner)
        self.assertEqual(order.team_id, self.team)
        self.assertEqual(order.user_id, self.salesperson)
        self.assertEqual(order.warehouse_id, self.warehouse_default)
        self.assertEqual(order.state, 'sale')
        self.assertEqual(len(order.order_line), 1)
        self.assertEqual(order.order_line.product_id, self.product)

    def test_order_line_records_its_own_meli_order_id(self):
        order_data = self._order_data(order_id='2000014726421999')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.order_line.mapped('meli_order_id'), ['2000014726421999'])

    def _pack_product(self, sku, name):
        product = self.env['product.product'].create({
            'name': name, 'company_id': self.test_company.id,
            # Explicit, not relying on whatever this shared database's
            # own default invoice_policy happens to be — this module's
            # own pack tests only care about SKU/line/chatter behavior,
            # never delivery-vs-invoicing timing.
            'invoice_policy': 'order',
        })
        self.env['meli.sku.mapping'].create({
            'product_id': product.id, 'meli_sku': sku,
        })
        return product

    def test_pack_siblings_consolidate_into_one_sale_order(self):
        # Fix 2026-09-18: these order/pack ids used to be real,
        # confirmed production values (S841155/S841156 already exist
        # in this shared database under this exact meli_order_id) —
        # _meli_create_from_order_data's own dedup check
        # (search by meli_order_id) silently returned those REAL orders
        # instead of ever creating a fresh one for this test, making
        # first_order/second_order two unrelated, already-separate real
        # sales that were never going to be equal. Every other test in
        # this class already uses made-up '20000191xxxxxxxx'-range ids
        # for exactly this reason — matched here too.
        self._pack_product('ZTEST-LO33NE', 'LONA 3 X 3 NEGRO')
        self._pack_product('ZTEST-LO33VE', 'LONA 3 X 3 VERDE')
        first_data = self._order_data(
            order_id='2000019200000001', sku='ZTEST-LO33NE', pack_id='2000019200099999',
        )
        second_data = self._order_data(
            order_id='2000019200000002', sku='ZTEST-LO33VE', pack_id='2000019200099999',
        )

        first_order = self.env['sale.order']._meli_create_from_order_data(self.config, first_data)
        second_order = self.env['sale.order']._meli_create_from_order_data(self.config, second_data)

        self.assertEqual(first_order, second_order)
        self.assertEqual(first_order.meli_pack_id, '2000019200099999')
        self.assertEqual(first_order.client_order_ref, '2000019200099999')
        self.assertEqual(
            sorted(first_order.order_line.mapped('meli_order_id')),
            ['2000019200000001', '2000019200000002'],
        )
        # Checks for the real distinguishing content ('same pack' plus
        # the sibling's own order id), not just 'Extra line' alone —
        # Odoo core itself posts its own unrelated "Extra line with
        # %s" chatter message for any new line added to a confirmed
        # order (sale/models/sale_order_line.py, create()), which would
        # make a looser assertion pass even if this module's own
        # message were never posted at all.
        self.assertTrue(any(
            'same pack' in (msg.body or '') and '2000019200000002' in (msg.body or '')
            for msg in first_order.message_ids
        ))

    def test_pack_sibling_retry_does_not_duplicate_the_line(self):
        self._pack_product('ZTEST-LO33NE', 'LONA 3 X 3 NEGRO')
        self._pack_product('ZTEST-LO33VE', 'LONA 3 X 3 VERDE')
        first_data = self._order_data(
            order_id='2000018335534650', sku='ZTEST-LO33NE', pack_id='2000014915601099',
        )
        second_data = self._order_data(
            order_id='2000018335534651', sku='ZTEST-LO33VE', pack_id='2000014915601099',
        )
        self.env['sale.order']._meli_create_from_order_data(self.config, first_data)
        order = self.env['sale.order']._meli_create_from_order_data(self.config, second_data)

        retried_order = self.env['sale.order']._meli_create_from_order_data(self.config, second_data)

        self.assertEqual(retried_order, order)
        self.assertEqual(len(order.order_line), 2)

    def test_pack_sibling_added_to_already_invoiced_order_gets_extra_note(self):
        self._pack_product('ZTEST-LO33NE', 'LONA 3 X 3 NEGRO')
        self._pack_product('ZTEST-LO33VE', 'LONA 3 X 3 VERDE')
        first_data = self._order_data(
            order_id='2000018335534660', sku='ZTEST-LO33NE', pack_id='2000014915601100',
        )
        second_data = self._order_data(
            order_id='2000018335534661', sku='ZTEST-LO33VE', pack_id='2000014915601100',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, first_data)
        # This class's own test_company never sets up a chart of
        # accounts (unlike TestMeliInvoicingLifecycle, which does) —
        # needed here since this is the only test in this class that
        # actually posts an invoice.
        # 'mx' (not 'mx_coa') confirmed as a valid chart-template code in
        # this Odoo version's registry — same one
        # TestMeliInvoicingLifecycle.setUpClass already uses.
        self.env['account.chart.template'].try_loading(
            'mx', company=self.test_company, install_demo=False,
        )
        invoice = order._create_invoices()
        invoice.action_post()

        self.env['sale.order']._meli_create_from_order_data(self.config, second_data)

        self.assertTrue(any(
            'may need manual invoicing' in (msg.body or '')
            for msg in order.message_ids
        ))

    def test_full_pack_sibling_line_auto_validates_its_own_picking(self):
        # Final branch review finding C2: a new sale.order.line added to
        # an already-confirmed Full order (via
        # _meli_add_pack_sibling_lines) triggers Odoo core's own
        # stock-rule logic and creates a brand NEW, separate
        # stock.picking for just that line — nobody at this company
        # ever touches a Full transfer manually (see
        # _meli_auto_validate_full_pickings's own docstring: Full orders
        # are fulfilled from Mercado Libre's own warehouse), so if that
        # second picking were left pending, the second sibling's line
        # would never show as delivered and stock would never be
        # decremented. Every picking on the consolidated order — the
        # first sibling's own picking AND the second sibling's new one
        # — must end up 'done'.
        first_product = self.env['product.product'].create({
            'name': 'Full Pack Sibling Product 1', 'type': 'product',
            'company_id': self.test_company.id,
        })
        second_product = self.env['product.product'].create({
            'name': 'Full Pack Sibling Product 2', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['stock.quant']._update_available_quantity(
            first_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        self.env['stock.quant']._update_available_quantity(
            second_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        self.env['meli.sku.mapping'].create({
            'product_id': first_product.id, 'meli_sku': 'SKU-FULL-PACK-1',
        })
        self.env['meli.sku.mapping'].create({
            'product_id': second_product.id, 'meli_sku': 'SKU-FULL-PACK-2',
        })
        first_data = self._order_data(
            order_id='2000018335599001', sku='SKU-FULL-PACK-1',
            pack_id='2000014915699001', shipping={'id': 999},
        )
        second_data = self._order_data(
            order_id='2000018335599002', sku='SKU-FULL-PACK-2',
            pack_id='2000014915699001', shipping={'id': 999},
        )
        # Same reasoning as test_full_order_auto_validates_the_picking:
        # both shipment-reading methods run whenever shipping.id is
        # truthy, and both siblings need to resolve as Full/fulfillment.
        # _meli_fetch_shipment_records (the shared fetch both of them
        # read from since the 2026-09-10 fix) is mocked too, for the
        # same reason — leaving it unmocked would hit the real API.
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, first_data)
            second_order = self.env['sale.order']._meli_create_from_order_data(
                self.config, second_data,
            )

        self.assertEqual(order, second_order)
        self.assertEqual(order.origin, 'XE-MLF-XEBRANDS')
        self.assertEqual(len(order.order_line), 2)
        self.assertTrue(order.picking_ids)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))

    def test_pack_order_shows_pack_id_as_client_reference_like_ventiapp(self):
        # Confirmed with the user 2026-09-01: when the order is part of a
        # Mercado Libre pack (cart), "OC Cliente"/"Ref. Cliente" must show
        # the pack ID instead of this order's own ID — matching Ventiapp's
        # own convention. meli_order_id always keeps the real, unique
        # order ID regardless, since every lookup keys on it instead.
        order_data = self._order_data(
            order_id='2000018233954784', pack_id='2000014819417533',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.client_order_ref, '2000014819417533')
        self.assertEqual(order.reference, '2000014819417533')
        self.assertEqual(order.meli_pack_id, '2000014819417533')
        self.assertEqual(order.meli_order_id, '2000018233954784')

    def test_order_dates_from_mercado_libre_are_parsed_to_utc(self):
        # Real question raised 2026-08-28: is an order that looks "old"
        # (small order id) actually a late payment (date_closed far after
        # date_created), or something else? These fields let us check
        # directly instead of guessing from the order id.
        order_data = self._order_data(
            order_id='2000014726421933',
            date_created='2026-08-01T10:01:50.000-06:00',
            date_closed='2026-08-28T09:04:07.000-06:00',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        # -06:00 (Mexico City) converted to naive UTC is +6 hours.
        self.assertEqual(str(order.meli_order_date_created), '2026-08-01 16:01:50')
        self.assertEqual(str(order.meli_order_date_closed), '2026-08-28 15:04:07')

    def test_missing_order_dates_leave_fields_empty(self):
        order_data = self._order_data(order_id='2000014726421934')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertFalse(order.meli_order_date_created)
        self.assertFalse(order.meli_order_date_closed)

    def test_date_order_uses_date_closed_not_date_created(self):
        # Decided with the user 2026-09-08: date_order (Odoo's own
        # native field, the one every standard sales report/filter
        # groups by) must reflect when the order was actually paid,
        # not when the cart was started — otherwise a late-paid order
        # (or one recovered days later) would misdate the sale.
        order_data = self._order_data(
            order_id='2000014726421935',
            date_created='2026-08-01T10:01:50.000-06:00',
            date_closed='2026-08-28T09:04:07.000-06:00',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(str(order.date_order), '2026-08-28 15:04:07')

    def test_date_order_falls_back_to_now_when_date_closed_is_missing(self):
        before = fields.Datetime.now()
        order_data = self._order_data(order_id='2000014726421936')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertGreaterEqual(order.date_order, before)

    def test_fulfillment_shipping_uses_fulfillment_warehouse(self):
        order_data = self._order_data(
            order_id='2000014726421912', shipping={'id': 999},
        )
        # Also mock _meli_fetch_custom_shipping_cost and the shared
        # _meli_fetch_shipment_records (2026-09-10 fix): a truthy
        # shipping.id makes _meli_create_from_order_data call all three,
        # and this test only cares about _meli_fetch_logistic_type —
        # leaving the others unmocked would hit the real Mercado Libre
        # API with the fake test credentials.
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.origin, 'XE-MLF-XEBRANDS')
        self.assertEqual(order.warehouse_id, self.warehouse_fulfillment)

    def test_unmapped_sku_leaves_order_in_draft(self):
        order_data = self._order_data(
            order_id='2000014726421913', sku='SKU-NOT-MAPPED',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'draft')
        self.assertFalse(order.order_line)
        self.assertTrue(any(
            'SKU-NOT-MAPPED' in (msg.body or '') for msg in order.message_ids
        ))

    def test_unmapped_sku_message_mentions_salesperson(self):
        order_data = self._order_data(
            order_id='2000014726421925', sku='SKU-NOT-MAPPED-MENTION',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        unmapped_message = order.message_ids.filtered(
            lambda m: 'SKU-NOT-MAPPED-MENTION' in (m.body or '')
        )
        self.assertTrue(unmapped_message)
        self.assertIn(self.salesperson.partner_id, unmapped_message.partner_ids)
        # Must be a real, visible @-mention in the message body (2026-08-28
        # user request) — not just a silent partner_ids notification.
        self.assertIn(
            f'data-oe-id="{self.salesperson.partner_id.id}"',
            unmapped_message.body,
        )
        self.assertIn(f'@{self.salesperson.name}', unmapped_message.body)

    def test_post_with_mention_can_target_an_explicit_partner(self):
        other_user = self.env['res.users'].create({
            'name': 'Returns Manager Test', 'login': 'returns_manager_mention_test',
        })
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
        })

        order._meli_post_with_mention(
            'A claim needs review.', mention_partner=other_user.partner_id,
        )

        message = order.message_ids[0]
        self.assertIn(f'data-oe-id="{other_user.partner_id.id}"', message.body)
        self.assertIn(other_user.partner_id.name, message.body)
        self.assertIn(other_user.partner_id.id, message.partner_ids.ids)

    def test_default_code_match_skips_the_mapping_table_entirely(self):
        direct_product = self.env['product.product'].create({
            'name': 'Producto con código directo', 'default_code': 'DIRECT-CODE-01',
        })
        order_data = self._order_data(
            order_id='2000014726421926', sku='DIRECT-CODE-01',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.order_line.product_id, direct_product)

    def test_retry_after_mapping_added_confirms_order(self):
        order_data = self._order_data(
            order_id='2000014726421920', sku='SKU-LATER-MAPPED',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        self.assertEqual(order.state, 'draft')

        new_product = self.env['product.product'].create({'name': 'Mapeado tarde'})
        self.env['meli.sku.mapping'].create({
            'product_id': new_product.id, 'meli_sku': 'SKU-LATER-MAPPED',
        })

        order._meli_retry_unmapped_lines(order_data)

        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.order_line.product_id, new_product)
        self.assertTrue(any(
            'confirmed' in (msg.body or '').lower() for msg in order.message_ids
        ))

    def test_retry_still_unmapped_stays_draft(self):
        order_data = self._order_data(
            order_id='2000014726421921', sku='SKU-STILL-MISSING',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        order._meli_retry_unmapped_lines(order_data)

        self.assertEqual(order.state, 'draft')
        self.assertFalse(order.order_line)

    def test_retry_is_noop_for_confirmed_orders(self):
        order_data = self._order_data(order_id='2000014726421922')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        self.assertEqual(order.state, 'sale')
        line_count_before = len(order.order_line)

        order._meli_retry_unmapped_lines(order_data)

        self.assertEqual(order.state, 'sale')
        self.assertEqual(len(order.order_line), line_count_before)

    def test_import_order_existing_draft_retries_via_entry_point(self):
        order_data = self._order_data(
            order_id='2000014726421923', sku='SKU-RETRY-VIA-ENTRY',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        self.assertEqual(order.state, 'draft')

        new_product = self.env['product.product'].create({'name': 'Retry entry point'})
        self.env['meli.sku.mapping'].create({
            'product_id': new_product.id, 'meli_sku': 'SKU-RETRY-VIA-ENTRY',
        })

        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000014726421923',
            )

        self.assertEqual(result, order)
        self.assertEqual(order.state, 'sale')

    def test_action_meli_retry_sku_mapping_button(self):
        order_data = self._order_data(
            order_id='2000014726421924', sku='SKU-BUTTON-RETRY',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        new_product = self.env['product.product'].create({'name': 'Botón retry'})
        self.env['meli.sku.mapping'].create({
            'product_id': new_product.id, 'meli_sku': 'SKU-BUTTON-RETRY',
        })

        with patch.object(type(self.config), '_api_get', return_value=order_data):
            order.action_meli_retry_sku_mapping()

        self.assertEqual(order.state, 'sale')

    def test_non_paid_status_is_not_imported(self):
        order_data = self._order_data(
            order_id='2000014726421914', status='confirmed',
        )
        result = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertFalse(result)
        self.assertFalse(self.env['sale.order'].search([
            ('client_order_ref', '=', '2000014726421914'),
        ]))

    def test_import_order_is_idempotent(self):
        existing = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'client_order_ref': '2000014726421915',
            'meli_order_id': '2000014726421915',
            'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_last_status': 'paid',
        })
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._order_data(order_id='2000014726421915'),
        ):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000014726421915',
            )
        self.assertEqual(result, existing)
        self.assertEqual(self.env['sale.order'].search_count([
            ('client_order_ref', '=', '2000014726421915'),
        ]), 1)

    # -- Recovery delay: give Ventiapp a chance before we auto-create
    # (2026-09-14 user request) --

    def test_recovery_of_a_recent_order_is_delayed_for_ventiapp(self):
        # Based on when it was PAID (date_closed), not date_created — see
        # _meli_order_recovery_delay_seconds's own docstring for why.
        recently_paid = fields.Datetime.now() - timedelta(minutes=3)
        order_data = self._order_data(
            order_id='2000019000000001',
            date_closed=recently_paid.isoformat() + '+00:00',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019000000001',
            )

        self.assertFalse(result)
        self.assertFalse(self.env['sale.order'].search([
            ('meli_order_id', '=', '2000019000000001'),
        ]))
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019000000001'),
        ])
        self.assertTrue(job, "the recovery must be re-queued, not dropped")
        self.assertTrue(job.eta)

    def test_recovery_of_a_recent_order_skips_the_delay_when_ventiapp_already_injected_it(self):
        ventiapp_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'reference': '2000019000000002', 'client_order_ref': '2000019000000002',
        })
        recently_paid = fields.Datetime.now() - timedelta(minutes=3)
        order_data = self._order_data(
            order_id='2000019000000002',
            date_closed=recently_paid.isoformat() + '+00:00',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019000000002',
            )

        self.assertEqual(result, ventiapp_order)
        self.assertEqual(result.meli_sync_source, 'xe_meli_connector')
        self.assertFalse(self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019000000002'),
        ]))

    def test_recovery_of_an_old_order_is_never_delayed(self):
        paid_long_ago = fields.Datetime.now() - timedelta(hours=2)
        order_data = self._order_data(
            order_id='2000019000000003',
            date_closed=paid_long_ago.isoformat() + '+00:00',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019000000003',
            )

        self.assertTrue(result)
        self.assertEqual(result.meli_order_id, '2000019000000003')
        # The point of the whole feature: a filter/report can now tell
        # this case apart from an order Ventiapp created or adopted.
        self.assertTrue(result.meli_auto_recovered)
        self.assertFalse(self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019000000003'),
        ]))

    def test_recovery_delay_falls_back_to_date_created_when_date_closed_missing(self):
        recently_created = fields.Datetime.now() - timedelta(minutes=3)
        order_data = self._order_data(
            order_id='2000019000000004',
            date_created=recently_created.isoformat() + '+00:00',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019000000004',
            )

        self.assertFalse(result)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019000000004'),
        ])
        self.assertTrue(job, "no date_closed at all must still fall back and delay")

    def test_adopted_order_never_gets_the_auto_recovered_flag(self):
        ventiapp_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'reference': '2000019000000005', 'client_order_ref': '2000019000000005',
        })
        old_paid = fields.Datetime.now() - timedelta(hours=2)
        order_data = self._order_data(
            order_id='2000019000000005',
            date_closed=old_paid.isoformat() + '+00:00',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019000000005',
            )

        self.assertEqual(result, ventiapp_order)
        self.assertFalse(result.meli_auto_recovered)

    # ---- Pack sibling discovery (Fix 2026-09-18) ----------------------
    # Real, critical production bug (order S970525/pack 2000014889797151):
    # this "invoicing-only" build only ever discovers an order via a
    # document's own meli_order_id (see controllers/main.py — the sale
    # injector webhook is deliberately not wired up here). A pack-wide
    # credit note reporting only ONE sibling's order_id left the OTHER
    # sibling's own product line permanently missing from Odoo, with no
    # error anywhere. _meli_import_order now also checks the pack itself
    # (GET /packs/$PACK_ID) and enqueues the missing sibling(s).

    def _fake_api_get_with_pack(self, orders_by_path, pack_id, pack_order_ids):
        def fake_api_get(path, params=None, headers=None):
            if path == f'/packs/{pack_id}':
                return {'orders': [{'id': oid} for oid in pack_order_ids]}
            if path in orders_by_path:
                return orders_by_path[path]
            raise AssertionError(f"unexpected path {path}")
        return fake_api_get

    def test_import_order_enqueues_a_missing_pack_sibling(self):
        order_data = self._order_data(
            order_id='2000019100000001', pack_id='2000019100099999',
        )
        fake_api_get = self._fake_api_get_with_pack(
            {'/orders/2000019100000001': order_data},
            '2000019100099999', ['2000019100000001', '2000019100000002'],
        )
        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000001',
            )

        self.assertTrue(result)
        job = self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019100000002'),
        ])
        self.assertTrue(job, "the missing sibling must be enqueued for import")

    def test_repair_pack_siblings_now_enqueues_missing_sibling(self):
        # _meli_repair_pack_siblings_now (2026-09-18 user request, the
        # per-order half of action_meli_repair_all_historical_packs):
        # same check-and-fix as any automated import, just callable
        # directly on an already-existing pack order.
        pack_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_pack_id': '2000019100099997',
            'meli_order_id': '2000019100000005',
        })
        fake_api_get = self._fake_api_get_with_pack(
            {}, '2000019100099997', ['2000019100000005', '2000019100000006'],
        )
        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            pack_order._meli_repair_pack_siblings_now(self.test_company.id)

        self.assertTrue(self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019100000006'),
        ]), "the missing sibling must be enqueued for import")

    def test_repair_pack_siblings_now_is_a_noop_when_pack_already_complete(self):
        pack_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_pack_id': '2000019100099996',
            'meli_order_id': '2000019100000007',
        })
        fake_api_get = self._fake_api_get_with_pack(
            {}, '2000019100099996', ['2000019100000007'],
        )
        # Counts jobs before/after instead of asserting none exist at
        # all: this test runs against a real, shared, non-isolated
        # database that already carries plenty of unrelated
        # meli_recover_order_* jobs from genuine production use.
        jobs_before = self.env['queue.job'].sudo().search_count([
            ('identity_key', 'ilike', 'meli_recover_order_%'),
        ])
        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            pack_order._meli_repair_pack_siblings_now(self.test_company.id)

        jobs_after = self.env['queue.job'].sudo().search_count([
            ('identity_key', 'ilike', 'meli_recover_order_%'),
        ])
        self.assertEqual(jobs_after, jobs_before, "no new job should have been enqueued")

    def test_action_meli_repair_all_historical_packs_queues_one_job_per_pack_order(self):
        pack_order_a = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_pack_id': '2000019100099995',
            'meli_order_id': '2000019100000008',
        })
        pack_order_b = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_pack_id': '2000019100099994',
            'meli_order_id': '2000019100000009',
        })
        non_pack_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
        })

        result = (pack_order_a | pack_order_b | non_pack_order).action_meli_repair_all_historical_packs()

        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertTrue(self.env['queue.job'].sudo().search([
            ('identity_key', '=', f'meli_pack_historical_repair_{pack_order_a.id}'),
        ]))
        self.assertTrue(self.env['queue.job'].sudo().search([
            ('identity_key', '=', f'meli_pack_historical_repair_{pack_order_b.id}'),
        ]))
        # A non-pack order still gets queued (this method decides
        # whether there's anything to do per-order, once the job runs —
        # cheap either way, no API call for a non-pack order at all).
        self.assertTrue(self.env['queue.job'].sudo().search([
            ('identity_key', '=', f'meli_pack_historical_repair_{non_pack_order.id}'),
        ]))

    def test_import_order_does_not_reenqueue_an_already_known_sibling_line(self):
        second_product = self.env['product.product'].create({
            'name': 'Sibling Product', 'company_id': self.test_company.id,
        })
        pack_order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_pack_id': '2000019100099998',
            'meli_order_id': '2000019100000003',
            'order_line': [(0, 0, {
                'product_id': second_product.id, 'product_uom_qty': 1,
                'meli_order_id': '2000019100000004',
            })],
        })
        order_data = self._order_data(
            order_id='2000019100000003', pack_id='2000019100099998',
        )
        fake_api_get = self._fake_api_get_with_pack(
            {'/orders/2000019100000003': order_data},
            '2000019100099998', ['2000019100000003', '2000019100000004'],
        )
        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000003',
            )

        self.assertFalse(self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019100000004'),
        ]))
        self.assertEqual(pack_order.meli_order_id, '2000019100000003')

    def test_import_order_without_pack_id_never_calls_the_packs_endpoint(self):
        order_data = self._order_data(order_id='2000019100000005', pack_id=None)

        def fake_api_get(path, params=None, headers=None):
            if path == '/orders/2000019100000005':
                return order_data
            raise AssertionError(f"unexpected path {path} — no pack_id, must never fetch /packs/")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000005',
            )

        self.assertTrue(result)

    def test_import_order_survives_a_failed_packs_fetch(self):
        order_data = self._order_data(
            order_id='2000019100000006', pack_id='2000019100099997',
        )

        def fake_api_get(path, params=None, headers=None):
            if path == '/orders/2000019100000006':
                return order_data
            if path == '/packs/2000019100099997':
                raise self._http_404()
            raise AssertionError(f"unexpected path {path}")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            result = self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000006',
            )

        self.assertTrue(result, "a failed pack lookup must never block the ordinary import")

    def test_import_order_rechecks_pack_siblings_even_when_the_order_already_existed(self):
        # The missing sibling can arrive at any time relative to the
        # first one — a later, otherwise-unrelated re-import of the SAME
        # already-known order (e.g. a status-change notification) must
        # also re-check the pack, not just the very first import.
        order_data = self._order_data(
            order_id='2000019100000007', pack_id='2000019100099996',
        )
        with patch.object(type(self.config), '_api_get', return_value=order_data):
            self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000007',
            )

        fake_api_get = self._fake_api_get_with_pack(
            {'/orders/2000019100000007': order_data},
            '2000019100099996', ['2000019100000007', '2000019100000008'],
        )
        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            self.env['sale.order']._meli_import_order(
                self.test_company.id, '2000019100000007',
            )

        self.assertTrue(self.env['queue.job'].sudo().search([
            ('identity_key', '=', 'meli_recover_order_2000019100000008'),
        ]))

    def test_missing_default_warehouse_raises_clear_error(self):
        # Real bug hit in production (2026-08-27): an unconfigured
        # warehouse_default_id used to bubble up as a raw Postgres
        # NotNullViolation on warehouse_id instead of a clear message.
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli no warehouse)'})
        config_no_warehouse = self.env['meli.config'].create({
            'company_id': other_company.id,
            'client_id': 'test-client-id-2',
            'client_secret': 'test-client-secret-2',
            'state': 'connected',
            'partner_id': self.partner.id,
        })
        order_data = self._order_data(order_id='2000014726421919')
        with self.assertRaises(UserError):
            self.env['sale.order']._meli_create_from_order_data(config_no_warehouse, order_data)
        self.assertFalse(self.env['sale.order'].search([
            ('client_order_ref', '=', '2000014726421919'),
        ]))

    def test_full_order_auto_validates_the_picking(self):
        storable_product = self.env['product.product'].create({
            'name': 'Producto Full Test', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['stock.quant']._update_available_quantity(
            storable_product, self.warehouse_fulfillment.lot_stock_id, 10,
        )
        self.env['meli.sku.mapping'].create({
            'product_id': storable_product.id, 'meli_sku': 'SKU-FULL-PICKING',
        })
        order_data = self._order_data(
            order_id='2000014726421929', sku='SKU-FULL-PICKING',
            shipping={'id': 999},
        )
        # Same reasoning as test_fulfillment_shipping_uses_fulfillment_warehouse:
        # both shipment-reading methods (and the shared
        # _meli_fetch_shipment_records fetch, 2026-09-10 fix) run
        # whenever shipping.id is truthy.
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value='fulfillment',
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'sale')
        self.assertTrue(order.picking_ids)
        self.assertTrue(all(p.state == 'done' for p in order.picking_ids))

    def test_non_full_order_leaves_picking_for_manual_handling(self):
        storable_product = self.env['product.product'].create({
            'name': 'Producto No Full Test', 'type': 'product',
            'company_id': self.test_company.id,
        })
        self.env['meli.sku.mapping'].create({
            'product_id': storable_product.id, 'meli_sku': 'SKU-NONFULL-PICKING',
        })
        order_data = self._order_data(
            order_id='2000014726421931', sku='SKU-NONFULL-PICKING',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'sale')
        self.assertTrue(order.picking_ids)
        self.assertTrue(all(p.state != 'done' for p in order.picking_ids))

    def test_ml_price_already_including_tax_is_not_taxed_twice(self):
        # Real bug found 2026-08-28: Mercado Libre's unit_price is the
        # tax-inclusive amount the buyer paid. Passing it straight through
        # as price_unit made Odoo add IVA a second time on top, inflating
        # the order total beyond what was actually charged.
        tax_group = self.env['account.tax.group'].create({'name': 'Test Tax Group 16%'})
        tax_16 = self.env['account.tax'].create({
            'name': 'IVA Test 16%', 'amount': 16.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'price_include': False,
            'company_id': self.test_company.id, 'tax_group_id': tax_group.id,
            'country_id': self.env.ref('base.mx').id,
        })
        self.product.taxes_id = [(6, 0, [tax_16.id])]
        order_data = self._order_data(order_id='2000014726421928')
        order_data['order_items'][0]['unit_price'] = 898.0
        # Fix 2026-09-18: pinned to 1 unit explicitly — _order_data's own
        # default quantity (2) made this assertion compare a per-UNIT
        # tax-included price (898.0) against the whole order's total
        # (which, at 2 units, is correctly 1796.0 — confirmed by hand:
        # 774.14 untaxed * 2 units + 16% tax = 1796.0). The test's own
        # intent (unit_price must not be taxed twice) only holds at 1
        # unit, where tax-included total == unit_price exactly.
        order_data['order_items'][0]['quantity'] = 1

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertAlmostEqual(order.amount_total, 898.0, places=2)
        self.assertAlmostEqual(order.order_line.price_unit, 774.14, places=2)

    def test_price_never_falls_back_to_catalog_list_price(self):
        # Real bug found 2026-08-28: falling back to Odoo's own
        # product.list_price when Mercado Libre's unit_price was low
        # produced totals with no relationship to what was actually sold.
        # The price must always come from ML, never product.list_price.
        # (Not testing unit_price=0: this database has an unrelated
        # xe_pacific constraint rejecting $0 order lines outright.)
        self.product.list_price = 999.0
        order_data = self._order_data(order_id='2000014726421932')
        order_data['order_items'][0]['unit_price'] = 1.16

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertAlmostEqual(order.order_line.price_unit, 1.0, places=2)

    def test_null_unit_price_falls_back_to_payments_total_split_by_quantity(self):
        # Real production bug (2026-09-18, order 2000018353085326):
        # Mercado Libre's 'meli_resale'/catalog orders report
        # order_items[].unit_price as null (never a real 0) — confirmed
        # against the live API. The real amount paid only lives in
        # payments[].transaction_amount. User-approved 2026-09-18: split
        # that total evenly across quantity, since ML gives no per-SKU
        # breakdown when this happens.
        order_data = self._order_data(order_id='2000018353085326')
        order_data['order_items'][0]['unit_price'] = None
        order_data['order_items'][0]['quantity'] = 2
        order_data['payments'] = [{
            'status': 'approved', 'transaction_amount': 232.0,
        }]

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertAlmostEqual(order.order_line.price_unit, 100.0, places=2)
        self.assertTrue(any(
            'no per-item price' in (msg.body or '') for msg in order.message_ids
        ))

    def test_null_unit_price_ignores_unapproved_payments(self):
        order_data = self._order_data(order_id='2000018353085327')
        order_data['order_items'][0]['unit_price'] = None
        order_data['payments'] = [
            {'status': 'approved', 'transaction_amount': 116.0},
            {'status': 'rejected', 'transaction_amount': 9999.0},
        ]

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertAlmostEqual(order.order_line.price_unit, 50.0, places=2)

    def test_null_unit_price_with_no_payments_falls_back_to_minimum_price(self):
        # User-approved 2026-09-18: when there is genuinely no price
        # signal at all (no unit_price, no usable payment), price the
        # line at $0.01 instead of blocking the whole order's creation
        # (xe_pacific's own restrict_unit_price_zero() would otherwise
        # reject it outright), and notify this company's configured
        # failure-notification users.
        order_data = self._order_data(order_id='2000018353085328')
        order_data['order_items'][0]['unit_price'] = None

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertAlmostEqual(order.order_line.price_unit, 0.01, places=2)
        self.assertTrue(any(
            'no usable price at all' in (msg.body or '') for msg in order.message_ids
        ))

    def test_import_no_longer_posts_a_price_debug_chatter_message(self):
        # Removed at the user's request 2026-09-07 — the price is still
        # forced correctly on the line (see the two tests above), just
        # never announced in the chatter anymore.
        order_data = self._order_data(order_id='2000014726421933')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertFalse(any(
            'Mercado Libre unit_price' in (msg.body or '') for msg in order.message_ids
        ))

    def test_meli_portal_url_built_from_order_id(self):
        order_data = self._order_data(order_id='2000014726421934')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(
            order.meli_portal_url,
            'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
            'mensajeria/2000014726421934',
        )

    def test_meli_portal_url_prefers_pack_id_over_order_id(self):
        # Found in practice 2026-09-08: the portal keys a pack order's
        # messaging thread by the PACK id, not the individual order id —
        # same wrinkle meli_pack_id itself exists for (see meli_claim.py's
        # matching fix on the same date).
        order_data = self._order_data(order_id='2000014726421934')
        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        order.meli_pack_id = '2000014849956519'

        self.assertEqual(
            order.meli_portal_url,
            'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
            'mensajeria/2000014849956519',
        )

    def test_meli_portal_url_blank_without_a_meli_order_id(self):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id, 'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
        })
        self.assertFalse(order.meli_portal_url)

    def test_order_cancelled_by_external_automation_does_not_crash_the_job(self):
        # Found in practice 2026-08-28: something else in this database
        # (a base_automation rule, most likely) moved a freshly-created
        # order out of 'draft' before our own action_confirm() ran,
        # making action_confirm() raise UserError. The fix doesn't try to
        # out-guess that automation — it just checks state before calling
        # action_confirm() and reports instead of crashing.
        order_data = self._order_data(order_id='2000014726421927')
        original_create = type(self.env['sale.order']).create

        def create_then_cancel(self_records, vals_list):
            records = original_create(self_records, vals_list)
            records.write({'state': 'cancel'})
            return records

        with patch.object(
            type(self.env['sale.order']), 'create',
            autospec=True, side_effect=create_then_cancel,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'cancel')
        self.assertTrue(any(
            'cancelled it automatically' in (msg.body or '') for msg in order.message_ids
        ))

    def test_import_order_without_connected_config_raises(self):
        other_company = self.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (no meli config)'})
        with self.assertRaises(UserError):
            self.env['sale.order']._meli_import_order(other_company.id, '9999999999')

    def test_status_change_to_cancelled_posts_chatter_message(self):
        existing = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'client_order_ref': '2000014726421916',
            'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_last_status': 'paid',
        })
        order_data = self._order_data(
            order_id='2000014726421916', status='cancelled',
        )
        order_data['cancel_detail'] = {
            'description': 'El comprador canceló la compra',
            'requested_by': 'buyer',
        }
        existing._meli_flag_status_change(order_data)

        self.assertEqual(existing.meli_last_status, 'cancelled')
        self.assertTrue(any(
            'cancel' in (msg.body or '').lower() for msg in existing.message_ids
        ))
        self.assertTrue(any(
            'El comprador canceló la compra' in (msg.body or '')
            for msg in existing.message_ids
        ))

    def test_status_change_to_same_status_does_not_repost(self):
        existing = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'client_order_ref': '2000014726421917',
            'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_last_status': 'cancelled',
        })
        message_count_before = len(existing.message_ids)
        order_data = self._order_data(
            order_id='2000014726421917', status='cancelled',
        )
        existing._meli_flag_status_change(order_data)

        self.assertEqual(len(existing.message_ids), message_count_before)

    def test_status_change_to_uninteresting_status_updates_tracking_silently(self):
        existing = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'client_order_ref': '2000014726421918',
            'company_id': self.test_company.id,
            'warehouse_id': self.warehouse_default.id,
            'meli_last_status': 'paid',
        })
        message_count_before = len(existing.message_ids)
        order_data = self._order_data(
            order_id='2000014726421918', status='partially_paid',
        )
        existing._meli_flag_status_change(order_data)

        self.assertEqual(existing.meli_last_status, 'partially_paid')
        self.assertEqual(len(existing.message_ids), message_count_before)

    def test_pack_sibling_status_change_notification_flags_manual_review(self):
        # Finding I1: a pack sibling's own later status-change
        # notification (cancelled/pending_cancel/partially_refunded)
        # used to be silently swallowed by the "not 'paid' yet, skipping
        # import" early-return, since that ran BEFORE the pack-detection
        # branch and never reached _meli_flag_status_change at all —
        # before Task 2 consolidated pack siblings into one sale.order,
        # each sibling had its own sale.order and got this notification
        # normally. Per-line CANCELLATION granularity (spec section 5)
        # stays paused for a non-Full order — the whole consolidated
        # pack sale.order still only ever gets one manual-review chatter
        # message, never an automatic cancel/return/credit-note, exactly
        # like before this fix.
        #
        # Fix 2026-09-18 (user-directed follow-up): the sibling's own
        # commercial LINE, though, is no longer skipped — it's added for
        # traceability even though the pack isn't Full (see
        # _meli_create_from_order_data's own docstring on this branch).
        # Only the destructive/automated side of a status change stays
        # paused; visibility into what was actually sold does not.
        self._pack_product('ZTEST-LO33NE', 'LONA 3 X 3 NEGRO')
        self._pack_product('ZTEST-LO33VE', 'LONA 3 X 3 VERDE')
        first_data = self._order_data(
            order_id='2000018335534700', sku='ZTEST-LO33NE', pack_id='2000014915601200',
        )
        second_data = self._order_data(
            order_id='2000018335534701', sku='ZTEST-LO33VE', pack_id='2000014915601200',
        )
        order = self.env['sale.order']._meli_create_from_order_data(self.config, first_data)
        self.env['sale.order']._meli_create_from_order_data(self.config, second_data)
        message_count_before = len(order.message_ids)

        # A THIRD notification, for an order id never seen before, part
        # of the same pack, reporting the sibling itself was cancelled
        # (never actually imported as a line — the notification alone
        # is enough to trigger the review message).
        cancelled_sibling_data = self._order_data(
            order_id='2000018335534702', sku='ZTEST-LO33NE',
            pack_id='2000014915601200', status='cancelled',
        )
        cancelled_sibling_data['cancel_detail'] = {
            'description': 'El comprador canceló la compra',
            'requested_by': 'buyer',
        }
        result = self.env['sale.order']._meli_create_from_order_data(
            self.config, cancelled_sibling_data,
        )

        self.assertEqual(result, order)
        self.assertEqual(order.meli_last_status, 'cancelled')
        self.assertGreater(len(order.message_ids), message_count_before)
        self.assertTrue(any(
            'cancel' in (msg.body or '').lower() for msg in order.message_ids
        ))
        self.assertTrue(any(
            'El comprador canceló la compra' in (msg.body or '')
            for msg in order.message_ids
        ))
        # The cancelled sibling's own line IS added now (traceability,
        # 2026-09-18) — but nothing else about the order was touched
        # automatically: still 'sale', not cancelled/returned/credited.
        self.assertTrue(self.env['sale.order.line'].search([
            ('meli_order_id', '=', '2000018335534702'),
        ]))
        self.assertEqual(order.state, 'sale')

    def test_fetch_custom_shipping_cost_reads_base_cost_for_custom_mode(self):
        # Real payload confirmed 2026-08-31 against Mercado Libre order
        # 2000018198314102 (a Bismarck security door sale): custom
        # shipments carry "mode": "custom" and "base_cost", and have no
        # "logistic_type" key at all.
        shipments_response = [{
            'id': 47893584846, 'type': 'forward', 'mode': 'custom',
            'base_cost': 900, 'order_cost': 4110.74, 'status': 'pending',
        }]
        order_data = self._order_data(
            order_id='2000018198314102', shipping={'id': 47893584846},
        )
        with patch.object(
            type(self.config), '_api_get', return_value=shipments_response,
        ) as mock_api_get:
            cost = self.env['sale.order']._meli_fetch_custom_shipping_cost(
                self.config, '2000018198314102', order_data,
            )

        self.assertEqual(cost, 900)
        mock_api_get.assert_called_once_with(
            '/orders/2000018198314102/shipments',
            headers={'X-New-Domain': 'true'},
        )

    def test_fetch_custom_shipping_cost_returns_none_for_fulfillment(self):
        shipments_response = [{
            'id': 1, 'type': 'forward', 'logistic_type': 'fulfillment',
        }]
        order_data = self._order_data(order_id='2000018198314103', shipping={'id': 1})
        with patch.object(type(self.config), '_api_get', return_value=shipments_response):
            cost = self.env['sale.order']._meli_fetch_custom_shipping_cost(
                self.config, '2000018198314103', order_data,
            )

        self.assertIsNone(cost)

    def test_fetch_custom_shipping_cost_returns_none_without_shipping_id(self):
        order_data = self._order_data(order_id='2000018198314104', shipping={})

        cost = self.env['sale.order']._meli_fetch_custom_shipping_cost(
            self.config, '2000018198314104', order_data,
        )

        self.assertIsNone(cost)

    def test_fetch_buyer_shipping_surcharge_reads_receiver_cost(self):
        # Real payload confirmed 2026-09-19/20 against Mercado Libre
        # order 2000018521677590/shipment 48044112162: a normal 'me2'
        # shipment (no 'custom') — GET /shipments/{id}/costs returns
        # receiver.cost (the BUYER's own share, $38.44 in that real
        # example) separate from senders[0].cost (XE's own net freight
        # expense, never used here — see this method's own docstring).
        shipments_response = [{
            'id': 48044112162, 'type': 'forward', 'mode': 'me2',
        }]
        costs_response = {
            'receiver': {'cost': 38.44},
            'senders': [{'cost': 46.0}],
        }
        order_data = self._order_data(
            order_id='2000018521677590', shipping={'id': 48044112162},
        )

        def fake_api_get(path, **kwargs):
            if path == '/orders/2000018521677590/shipments':
                return shipments_response
            if path == '/shipments/48044112162/costs':
                return costs_response
            raise AssertionError(f"unexpected path {path}")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            cost = self.env['sale.order']._meli_fetch_buyer_shipping_surcharge(
                self.config, '2000018521677590', order_data,
            )

        self.assertEqual(cost, 38.44)

    def test_fetch_buyer_shipping_surcharge_skips_custom_shipments(self):
        # Never double-charge the buyer for freight: a 'custom' shipment
        # is already covered by _meli_fetch_custom_shipping_cost — this
        # method must return None WITHOUT even calling /costs for it.
        shipments_response = [{
            'id': 1, 'type': 'forward', 'mode': 'custom', 'base_cost': 900,
        }]
        order_data = self._order_data(order_id='2000018521677591', shipping={'id': 1})

        with patch.object(
            type(self.config), '_api_get', return_value=shipments_response,
        ) as mock_api_get:
            cost = self.env['sale.order']._meli_fetch_buyer_shipping_surcharge(
                self.config, '2000018521677591', order_data,
            )

        self.assertIsNone(cost)
        mock_api_get.assert_called_once_with(
            '/orders/2000018521677591/shipments',
            headers={'X-New-Domain': 'true'},
        )

    def test_fetch_buyer_shipping_surcharge_skips_catalog_orders(self):
        # Fix 2026-09-21 (real production case caught before import,
        # order 2000018568677372/pack 2000015136237497): a catalog/
        # resale order's own 'tags' always includes 'catalog' — what
        # Mercado Libre actually owes the seller for one of these never
        # includes the buyer's own shipping share (that's Mercado
        # Libre's own resale markup), so this must return None WITHOUT
        # even calling the API — a real mock_api_get call here would
        # mean the fix regressed.
        order_data = self._order_data(order_id='2000018568677372', shipping={'id': 1})
        order_data['tags'] = ['order_has_discount', 'catalog', 'paid', 'pack_order']

        with patch.object(type(self.config), '_api_get') as mock_api_get:
            cost = self.env['sale.order']._meli_fetch_buyer_shipping_surcharge(
                self.config, '2000018568677372', order_data,
            )

        self.assertIsNone(cost)
        mock_api_get.assert_not_called()

    def test_fetch_buyer_shipping_surcharge_reraises_request_exception(self):
        shipments_response = [{'id': 1, 'type': 'forward', 'mode': 'me2'}]
        order_data = self._order_data(order_id='2000018521677592', shipping={'id': 1})

        def fake_api_get(path, **kwargs):
            if path == '/orders/2000018521677592/shipments':
                return shipments_response
            raise requests.exceptions.ConnectionError('boom')

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            with self.assertRaises(requests.exceptions.RequestException):
                self.env['sale.order']._meli_fetch_buyer_shipping_surcharge(
                    self.config, '2000018521677592', order_data,
                )

    def test_buyer_shipping_surcharge_adds_a_line_to_a_new_order(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Test Shipping Surcharge (buyer)', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(
            order_id='2000018521677593', shipping={'id': 48044112163},
        )
        shipments_response = [{
            'id': 48044112163, 'type': 'forward', 'mode': 'me2',
        }]
        costs_response = {'receiver': {'cost': 38.44}, 'senders': [{'cost': 46.0}]}

        def fake_api_get(path, **kwargs):
            if path == '/orders/2000018521677593/shipments':
                return shipments_response
            if path == '/shipments/48044112163/costs':
                return costs_response
            raise AssertionError(f"unexpected path {path}")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(len(order.order_line), 2)
        shipping_line = order.order_line.filtered(lambda l: l.product_id == shipping_item)
        self.assertEqual(len(shipping_line), 1)
        self.assertEqual(shipping_line.product_uom_qty, 1)
        self.assertAlmostEqual(shipping_line.price_unit, 38.44 / 1.16, places=2)

    def test_custom_shipping_adds_surcharge_line_with_untaxed_price(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Test Shipping Surcharge', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(
            order_id='2000018198314105', shipping={'id': 47893584846},
        )

        # Also mock _meli_fetch_logistic_type, the shared
        # _meli_fetch_shipment_records (2026-09-10 fix), and
        # _meli_fetch_custom_shipping_destination (its own separate
        # /shipments/{id} fetch, never covered by the above): a truthy
        # shipping.id makes _meli_create_from_order_data call all four
        # — leaving any unmocked would hit the real Mercado Libre API
        # with the fake test credentials. None is exactly what this
        # method itself returns on any fetch failure — safe, and
        # irrelevant to what this test actually asserts (the shipping
        # surcharge line).
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value=None,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(len(order.order_line), 2)
        shipping_line = order.order_line.filtered(lambda l: l.product_id == shipping_item)
        self.assertEqual(len(shipping_line), 1)
        self.assertEqual(shipping_line.product_uom_qty, 1)
        self.assertAlmostEqual(shipping_line.price_unit, 900 / 1.16, places=2)

    def test_custom_shipping_without_configured_item_raises_error(self):
        self.config.shipping_item_id = False
        order_data = self._order_data(order_id='2000018198314106', shipping={'id': 1})

        # Same reasoning as
        # test_custom_shipping_adds_surcharge_line_with_untaxed_price:
        # both shipment-reading methods (and the shared fetch) run
        # whenever shipping.id is truthy.
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value=None,
        ):
            with self.assertRaises(UserError):
                self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

    def test_non_custom_shipping_does_not_add_a_surcharge_line(self):
        order_data = self._order_data(order_id='2000018198314107')

        order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(len(order.order_line), 1)

    def test_fetch_custom_shipping_cost_reraises_request_exception(self):
        # Final branch review finding (Important #1, 2026-08-31): a
        # RequestException here used to be swallowed into None — the same
        # value returned for "this order isn't custom shipping". The
        # caller (_meli_create_from_order_data) needs to be able to tell
        # the two apart, so this method must now let it propagate.
        order_data = self._order_data(
            order_id='2000018198314108', shipping={'id': 1},
        )
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.RequestException('boom'),
        ):
            with self.assertRaises(requests.exceptions.RequestException):
                self.env['sale.order']._meli_fetch_custom_shipping_cost(
                    self.config, '2000018198314108', order_data,
                )

    def test_shipment_fetch_failure_retries_instead_of_creating_an_incomplete_order(self):
        # Fix 2026-09-10: this used to create the order anyway, without
        # the surcharge, and just leave a chatter warning — found in
        # production to genuinely lose a real freight surcharge when the
        # (now-removed) second, independent shipment fetch happened to
        # be the one that failed. Now a fetch failure retries the whole
        # import (via RetryableJobError, queue_job's own retry
        # machinery) instead of ever creating an order with unknown
        # shipping details.
        order_data = self._order_data(
            order_id='2000018198314109', shipping={'id': 1},
        )
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.RequestException('boom'),
        ):
            with self.assertRaises(RetryableJobError):
                self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertFalse(self.env['sale.order'].search([
            ('meli_order_id', '=', '2000018198314109'),
        ]))

    # -- Transient DB conflict during action_confirm() must retry, not
    # strand the order in draft forever (2026-09-14 real incident,
    # order S841238: "no se pudo serializar el acceso debido a un
    # update concurrente") --

    def test_confirm_serialization_failure_retries_instead_of_stranding_in_draft(self):
        db_error = _fake_operational_error(errorcodes.SERIALIZATION_FAILURE)
        order_data = self._order_data(order_id='2000019000000010')
        with patch.object(
            type(self.env['sale.order']), 'action_confirm', side_effect=db_error,
        ):
            with self.assertRaises(RetryableJobError):
                self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        # Rolled back with the rest of the (savepoint-wrapped) attempt —
        # matches the existing shipment-fetch-failure test's own
        # expectation just above: a retryable failure must not leave a
        # half-created order behind either.
        self.assertFalse(self.env['sale.order'].search([
            ('meli_order_id', '=', '2000019000000010'),
        ]))

    def test_confirm_non_concurrency_db_error_still_leaves_it_in_draft(self):
        db_error = _fake_operational_error(errorcodes.NOT_NULL_VIOLATION)
        order_data = self._order_data(order_id='2000019000000011')
        with patch.object(
            type(self.env['sale.order']), 'action_confirm', side_effect=db_error,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertEqual(order.state, 'draft')

    # -- Fallback to /shipments/{id} when /orders/{id}/shipments 404s
    # (2026-09-14 real incident, order 2000018458354168: that endpoint
    # 404'd on a genuinely real, Full/fulfillment shipment) --

    def _http_404(self):
        response = MagicMock()
        response.status_code = 404
        return requests.exceptions.HTTPError(response=response)

    def test_fetch_shipment_records_falls_back_on_404(self):
        order_data = {'id': '2000018458354168', 'shipping': {'id': 48013848567}}

        def fake_api_get(path, params=None, headers=None):
            if path == '/orders/2000018458354168/shipments':
                raise self._http_404()
            if path == '/shipments/48013848567':
                return {'logistic': {'mode': 'me2', 'type': 'fulfillment', 'direction': 'forward'}}
            raise AssertionError(f"unexpected path {path}")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            records = self.env['sale.order']._meli_fetch_shipment_records(
                self.config, '2000018458354168', order_data,
            )

        self.assertEqual(records, [{
            'type': 'forward', 'mode': 'me2', 'logistic_type': 'fulfillment',
        }])
        # The exact bug this fixes: logistic_type must resolve correctly
        # through the fallback, same as it would through the primary
        # endpoint — a Full order must never get silently reclassified
        # as non-Full just because this one endpoint 404'd on it.
        self.assertEqual(
            self.env['sale.order']._meli_fetch_logistic_type(
                self.config, '2000018458354168', order_data, shipments=records,
            ),
            'fulfillment',
        )

    def test_fetch_shipment_records_fallback_recovers_custom_shipping_cost(self):
        order_data = {'id': '2000018198314102', 'shipping': {'id': 47893584846}}

        def fake_api_get(path, params=None, headers=None):
            if path == '/orders/2000018198314102/shipments':
                raise self._http_404()
            if path == '/shipments/47893584846':
                return {'logistic': {'mode': 'custom', 'type': None, 'direction': 'forward'}}
            if path == '/shipments/47893584846/costs':
                return {'gross_amount': 900}
            raise AssertionError(f"unexpected path {path}")

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            records = self.env['sale.order']._meli_fetch_shipment_records(
                self.config, '2000018198314102', order_data,
            )
            cost = self.env['sale.order']._meli_fetch_custom_shipping_cost(
                self.config, '2000018198314102', order_data, shipments=records,
            )

        self.assertEqual(cost, 900)

    def test_fetch_shipment_records_returns_empty_when_fallback_also_404s(self):
        order_data = {'id': '2000018458354199', 'shipping': {'id': 999}}

        def fake_api_get(path, params=None, headers=None):
            raise self._http_404()

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            records = self.env['sale.order']._meli_fetch_shipment_records(
                self.config, '2000018458354199', order_data,
            )

        self.assertEqual(records, [])

    def test_fetch_shipment_records_propagates_non_404_http_errors(self):
        order_data = {'id': '2000018458354200', 'shipping': {'id': 999}}
        response = MagicMock()
        response.status_code = 500
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.HTTPError(response=response),
        ):
            with self.assertRaises(requests.exceptions.HTTPError):
                self.env['sale.order']._meli_fetch_shipment_records(
                    self.config, '2000018458354200', order_data,
                )

    def test_configure_shipping_item_error_names_the_order(self):
        # Final branch review finding (Minor #5): the UserError must name
        # the Mercado Libre order that tripped it, so an admin looking at
        # a failed queue_job knows which order needs configuration.
        self.config.shipping_item_id = False
        order_data = self._order_data(
            order_id='2000018198314110', shipping={'id': 1},
        )
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value=None,
        ):
            with self.assertRaises(UserError) as err_ctx:
                self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertIn('2000018198314110', str(err_ctx.exception))

    def test_custom_shipping_note_shows_custom_instead_of_na(self):
        # Final branch review finding (Minor #6): the order's note must
        # reflect that the shipment is custom, instead of rendering the
        # same "Logistics: N/A" a genuinely unknown shipment would get.
        shipping_item = self.env['product.product'].create({
            'name': 'Test Shipping Surcharge Note', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(
            order_id='2000018198314111', shipping={'id': 47893584846},
        )
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_logistic_type',
            return_value=None,
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)

        self.assertIn('custom', order.note)
        self.assertNotIn('N/A', order.note)

    def test_fetch_custom_shipping_destination_reads_receiver_and_address(self):
        shipment_response = {
            'destination': {
                'receiver_name': 'Juan Pérez',
                'receiver_phone': '8112345678',
                'shipping_address': {
                    'street_name': 'Calle Falsa',
                    'street_number': '123',
                    'city': {'id': 'X', 'name': 'Monterrey'},
                    'state': {'id': 'NLE', 'name': 'Nuevo León'},
                    'zip_code': '64000',
                    'country': {'id': 'MX', 'name': 'México'},
                },
            },
        }
        with patch.object(
            type(self.config), '_api_get', return_value=shipment_response,
        ) as mock_api_get:
            destination = self.env['sale.order']._meli_fetch_custom_shipping_destination(
                self.config, 999999,
            )

        mock_api_get.assert_called_once_with(
            '/shipments/999999', headers={'x-format-new': 'true'},
        )
        self.assertEqual(destination, {
            'name': 'Juan Pérez',
            'phone': '8112345678',
            'street': 'Calle Falsa 123',
            'city': 'Monterrey',
            'zip': '64000',
            'state_name': 'Nuevo León',
            'state_code': 'NLE',
            'country_code': 'MX',
        })

    def test_fetch_custom_shipping_destination_returns_none_without_receiver_name(self):
        shipment_response = {'destination': {'receiver_name': '', 'shipping_address': {}}}
        with patch.object(type(self.config), '_api_get', return_value=shipment_response):
            destination = self.env['sale.order']._meli_fetch_custom_shipping_destination(
                self.config, 999999,
            )
        self.assertIsNone(destination)

    def test_fetch_custom_shipping_destination_returns_none_on_request_exception(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=requests.exceptions.ConnectionError("boom"),
        ):
            destination = self.env['sale.order']._meli_fetch_custom_shipping_destination(
                self.config, 999999,
            )
        self.assertIsNone(destination)

    def test_custom_shipping_sets_delivery_contact_as_shipping_address(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(
            shipping={'id': 999}, buyer_id=555,
        )
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value={
                'name': 'Juan Pérez', 'phone': '8112345678',
                'street': 'Calle Falsa 123', 'city': 'Monterrey',
                'zip': '64000', 'state_name': 'Nuevo León',
                'state_code': 'NLE', 'country_code': 'MX',
            },
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.partner_id, self.partner)
        self.assertEqual(order.partner_shipping_id.name, 'Juan Pérez')
        self.assertEqual(order.partner_shipping_id.type, 'delivery')
        self.assertEqual(order.partner_shipping_id.phone, '8112345678')
        self.assertEqual(order.partner_shipping_id.parent_id.meli_buyer_id, '555')
        # The whole point of using partner_shipping_id (rather than some
        # custom field) is that Odoo copies it onto the stock.picking it
        # generates on confirmation — verify that actually happens, not
        # just that the field is set on the order.
        self.assertEqual(order.picking_ids.partner_id, order.partner_shipping_id)

    def test_custom_shipping_without_destination_falls_back_to_generic_contact(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(shipping={'id': 999}, buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.partner_shipping_id, self.partner)

    def test_custom_shipping_records_resolved_status_and_ids(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(shipping={'id': 999}, buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value={
                'name': 'Juan Pérez', 'phone': '8112345678',
                'street': 'Calle Falsa 123', 'city': 'Monterrey',
                'zip': '64000', 'state_name': 'Nuevo León',
                'state_code': 'NLE', 'country_code': 'MX',
            },
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.meli_delivery_contact_status, 'resolved')
        self.assertEqual(order.meli_shipping_id, '999')
        self.assertEqual(order.meli_buyer_id, '555')

    def test_custom_shipping_records_failed_status_and_notifies_manager(self):
        manager = self.env['res.users'].create({
            'name': 'Delivery Contact Manager Test',
            'login': 'delivery_contact_manager_test',
        })
        self.config.delivery_contact_manager_id = manager
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(shipping={'id': 999}, buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.meli_delivery_contact_status, 'failed')
        self.assertEqual(order.meli_shipping_id, '999')
        self.assertEqual(order.meli_buyer_id, '555')
        self.assertEqual(order.partner_shipping_id, self.partner)
        message = order.message_ids[0]
        self.assertIn(manager.partner_id.id, message.partner_ids.ids)

    def test_non_custom_shipping_status_stays_not_applicable(self):
        order_data = self._order_data(buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.meli_delivery_contact_status, 'not_applicable')
        self.assertFalse(order.meli_shipping_id)
        self.assertFalse(order.meli_buyer_id)

    def test_full_order_still_captures_shipping_id_for_reporting(self):
        # Found in practice 2026-09-08: meli_shipping_id used to be
        # extracted only inside the custom-shipping branch, so a Full/
        # fulfillment order (the vast majority of orders) never recorded
        # it even though Mercado Libre's own order resource always
        # carries shipping.id — needed for a Google Sheets report that
        # isn't limited to custom-shipping orders.
        order_data = self._order_data(shipping={'id': 4791})
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        self.assertEqual(order.meli_delivery_contact_status, 'not_applicable')
        self.assertEqual(order.meli_shipping_id, '4791')
        # buyer_id extraction/delivery-contact resolution stays scoped to
        # custom shipping — a Full order has no use for it.
        self.assertFalse(order.meli_buyer_id)

    def test_non_custom_shipping_never_fetches_a_delivery_destination(self):
        order_data = self._order_data(buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
        ) as mock_fetch_destination:
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )

        mock_fetch_destination.assert_not_called()
        self.assertEqual(order.partner_shipping_id, self.partner)

    def test_retry_delivery_contact_resolves_and_posts_a_success_message(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(shipping={'id': 999}, buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )
        self.assertEqual(order.meli_delivery_contact_status, 'failed')
        messages_before = order.message_ids

        with patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value={
                'name': 'Juan Pérez', 'phone': '8112345678',
                'street': 'Calle Falsa 123', 'city': 'Monterrey',
                'zip': '64000', 'state_name': 'Nuevo León',
                'state_code': 'NLE', 'country_code': 'MX',
            },
        ):
            order._meli_retry_delivery_contact(self.config)

        self.assertEqual(order.meli_delivery_contact_status, 'resolved')
        self.assertEqual(order.partner_shipping_id.name, 'Juan Pérez')
        self.assertTrue(len(order.message_ids) > len(messages_before))

    def test_retry_delivery_contact_still_failing_does_not_repost_the_warning(self):
        shipping_item = self.env['product.product'].create({
            'name': 'Recargo de envío', 'type': 'service',
            'company_id': self.test_company.id,
        })
        self.config.shipping_item_id = shipping_item
        order_data = self._order_data(shipping={'id': 999}, buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_shipment_records',
            return_value=[],
        ), patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=900.0,
        ), patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(
                self.config, order_data,
            )
        messages_before = order.message_ids

        with patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
            return_value=None,
        ):
            order._meli_retry_delivery_contact(self.config)

        self.assertEqual(order.meli_delivery_contact_status, 'failed')
        self.assertEqual(order.message_ids, messages_before)

    def test_retry_delivery_contact_is_a_noop_when_not_failed(self):
        order_data = self._order_data(buyer_id=555)
        with patch.object(
            type(self.env['sale.order']), '_meli_fetch_custom_shipping_cost',
            return_value=None,
        ):
            order = self.env['sale.order']._meli_create_from_order_data(self.config, order_data)
        self.assertEqual(order.meli_delivery_contact_status, 'not_applicable')

        with patch.object(
            type(self.env['sale.order']),
            '_meli_fetch_custom_shipping_destination',
        ) as mock_fetch:
            order._meli_retry_delivery_contact(self.config)

        mock_fetch.assert_not_called()
