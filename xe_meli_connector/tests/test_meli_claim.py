from unittest.mock import patch

import requests

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliClaim(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli claims)'})
        cls.returns_manager = cls.env['res.users'].create({
            'name': 'Returns Manager Test', 'login': 'meli_claim_returns_manager',
        })
        cls.warehouse_full = cls.env['stock.warehouse'].create({
            'name': 'Almacén Full Claim Test', 'code': 'FCLT',
            'company_id': cls.test_company.id,
        })
        cls.warehouse_other = cls.env['stock.warehouse'].create({
            'name': 'Almacén No-Full Claim Test', 'code': 'NCLT',
            'company_id': cls.test_company.id,
        })
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'claim-client', 'client_secret': 'claim-secret',
            'state': 'connected',
            'warehouse_fulfillment_id': cls.warehouse_full.id,
            'returns_manager_id': cls.returns_manager.id,
        })
        cls.partner = cls.env['res.partner'].create({'name': 'Meli Claim Test Buyer'})
        cls.full_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id, 'company_id': cls.test_company.id,
            'client_order_ref': 'CLAIM-ORDER-FULL',
            'meli_order_id': 'CLAIM-ORDER-FULL',
            'warehouse_id': cls.warehouse_full.id,
        })
        cls.non_full_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id, 'company_id': cls.test_company.id,
            'client_order_ref': 'CLAIM-ORDER-NONFULL',
            'meli_order_id': 'CLAIM-ORDER-NONFULL',
            'warehouse_id': cls.warehouse_other.id,
        })
        # A Ventiapp-style legacy order: client_order_ref only, never
        # meli_order_id — the case _compute_sale_order_id's fallback exists for.
        cls.legacy_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id, 'company_id': cls.test_company.id,
            'client_order_ref': 'CLAIM-ORDER-LEGACY',
            'warehouse_id': cls.warehouse_full.id,
        })
        # A Ventiapp-style legacy PACK order: client_order_ref holds the
        # PACK id (Ventiapp's own convention), never the individual order
        # id a claim's resource_id always is — the case the meli_pack_id
        # fallback exists for.
        cls.legacy_pack_order = cls.env['sale.order'].create({
            'partner_id': cls.partner.id, 'company_id': cls.test_company.id,
            'client_order_ref': 'CLAIM-PACK-LEGACY',
            'warehouse_id': cls.warehouse_full.id,
        })

    def _claim_payload(self, claim_id='5000000001', claim_type='return',
                        status='opened', resource='order', resource_id='CLAIM-ORDER-NONFULL',
                        stage='claim', resolution=None, reason_id=None,
                        date_created='2024-03-14T08:28:44.000-04:00'):
        return {
            'id': claim_id, 'type': claim_type, 'status': status,
            'resource': resource, 'resource_id': resource_id, 'stage': stage,
            'resolution': resolution, 'reason_id': reason_id,
            'date_created': date_created,
        }

    def _dispatch_api_get(
        self, claim_payload, reason_payload=None, reputation_payload=None,
        order_payload=None,
    ):
        # config._api_get() is called with a single path for the claim
        # itself, and — since 2026-09-08 — also for the reason lookup
        # (/claims/reasons/$CODE), the reputation check
        # (/claims/$ID/affects-reputation), and (since 2026-09-07) the
        # pack_id fetch (/orders/$ORDER_ID). Route each by path so a
        # single patch.object(..., side_effect=...) can cover all four.
        def fake_api_get(path, params=None, headers=None):
            if '/affects-reputation' in path:
                return reputation_payload or {}
            if '/claims/reasons/' in path:
                return reason_payload or {}
            if path.startswith('/orders/'):
                return order_payload or {}
            return claim_payload
        return fake_api_get

    def test_import_claim_creates_record_for_in_scope_type(self):
        with patch.object(
            type(self.config), '_api_get', return_value=self._claim_payload(),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000001',
            )

        self.assertTrue(claim)
        self.assertEqual(claim.claim_id, '5000000001')
        self.assertEqual(claim.claim_type, 'return')
        self.assertEqual(claim.claim_status, 'opened')
        self.assertEqual(claim.sale_order_id, self.non_full_order)
        self.assertFalse(claim.is_full)

        # sale.order side of the relation: the smart button's count field
        # and its underlying One2many must see the same claim.
        self.assertEqual(self.non_full_order.meli_claim_count, 1)
        self.assertEqual(self.non_full_order.meli_claim_ids, claim)
        self.assertEqual(self.full_order.meli_claim_count, 0)

        action = self.non_full_order.action_open_meli_claims()
        self.assertEqual(action['res_model'], 'meli.claim')
        self.assertEqual(action['domain'], [('sale_order_id', '=', self.non_full_order.id)])

    def test_meli_portal_url_built_from_order_id_and_claim_id(self):
        with patch.object(
            type(self.config), '_api_get', return_value=self._claim_payload(),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000001',
            )

        self.assertEqual(
            claim.meli_portal_url,
            'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
            'mensajeria/CLAIM-ORDER-NONFULL/reclamo/5000000001',
        )

    def test_import_claim_ignores_out_of_scope_type(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000002', claim_type='mediations'),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000002',
            )

        self.assertFalse(claim)
        self.assertFalse(self.env['meli.claim'].sudo().search([('claim_id', '=', '5000000002')]))

    def test_import_claim_ignores_non_order_resource(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000003', resource='payment'),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000003',
            )

        self.assertFalse(claim)

    def test_import_claim_upserts_by_claim_id(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000004', status='opened'),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000004')

        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000004', status='closed'),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000004')

        claims = self.env['meli.claim'].sudo().search([('claim_id', '=', '5000000004')])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims.claim_status, 'closed')

    def test_import_claim_computes_is_full_from_order_warehouse(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000005', resource_id='CLAIM-ORDER-FULL',
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000005',
            )

        self.assertEqual(claim.sale_order_id, self.full_order)
        self.assertTrue(claim.is_full)

    def test_import_claim_notifies_returns_manager_when_non_full_and_newly_opened(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000006'),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000006')

        message = self.non_full_order.message_ids[0]
        self.assertIn(f'data-oe-id="{self.returns_manager.partner_id.id}"', message.body)

    def test_import_claim_does_not_notify_when_full(self):
        message_count_before = len(self.full_order.message_ids)
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000007', resource_id='CLAIM-ORDER-FULL',
            ),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000007')

        self.assertEqual(len(self.full_order.message_ids), message_count_before)

    def test_import_claim_does_not_renotify_on_repeat_open_status(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000008'),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000008')
        message_count_after_first = len(self.non_full_order.message_ids)

        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000008', stage='dispute'),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000008')

        self.assertEqual(len(self.non_full_order.message_ids), message_count_after_first)

    def test_import_claim_without_returns_manager_configured_does_not_crash(self):
        self.config.returns_manager_id = False
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000009'),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000009',
            )

        self.assertTrue(claim)

    def test_import_claim_accepts_plural_returns_type_and_stores_canonical(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000010', claim_type='returns',
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000010',
            )

        self.assertTrue(claim)
        self.assertEqual(claim.claim_type, 'return')

    def test_claim_company_id_matches_sale_order_company(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(claim_id='5000000011'),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000011',
            )

        self.assertEqual(claim.company_id, self.non_full_order.company_id)
        self.assertEqual(claim.company_id, self.test_company)

    def test_import_claim_matches_legacy_order_by_client_order_ref(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000012', resource_id='CLAIM-ORDER-LEGACY',
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000012',
            )

        self.assertEqual(claim.sale_order_id, self.legacy_order)
        # The legacy order sits in the Full warehouse — is_full must be
        # computed correctly too, not just sale_order_id.
        self.assertTrue(claim.is_full)

    def test_import_claim_fetches_pack_id_when_order_has_one(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000030', resource_id='CLAIM-ORDER-FULL'),
                order_payload={'pack_id': 2000014407292449},
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000030',
            )

        self.assertEqual(claim.meli_pack_id, '2000014407292449')

    def test_import_claim_pack_id_stays_blank_when_order_has_none(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000031', resource_id='CLAIM-ORDER-FULL'),
                order_payload={'pack_id': None},
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000031',
            )

        self.assertFalse(claim.meli_pack_id)

    def test_pack_id_fetch_failure_does_not_block_import(self):
        def fake_api_get(path, params=None, headers=None):
            if path.startswith('/orders/'):
                raise requests.exceptions.RequestException("ML is down")
            if '/affects-reputation' in path or '/claims/reasons/' in path:
                return {}
            return self._claim_payload(claim_id='5000000032', resource_id='CLAIM-ORDER-FULL')

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000032',
            )

        self.assertTrue(claim)
        self.assertEqual(claim.sale_order_id, self.full_order)
        self.assertFalse(claim.meli_pack_id)

    def test_import_claim_matches_legacy_pack_order_via_pack_id_fallback(self):
        # resource_id is the individual order id (never matches any
        # order here), but the fetched pack_id matches what a legacy
        # Ventiapp pack order stored in client_order_ref.
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(
                    claim_id='5000000033', resource_id='7000000000000099',
                ),
                order_payload={'pack_id': 'CLAIM-PACK-LEGACY'},
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000033',
            )

        self.assertEqual(claim.meli_pack_id, 'CLAIM-PACK-LEGACY')
        self.assertEqual(claim.sale_order_id, self.legacy_pack_order)
        # The legacy pack order sits in the Full warehouse too.
        self.assertTrue(claim.is_full)

    def test_import_claim_parses_claim_date_created_from_api(self):
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000034', date_created='2024-03-14T08:28:44.000-04:00',
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000034',
            )

        self.assertEqual(
            claim.claim_date_created, fields.Datetime.to_datetime('2024-03-14 12:28:44'),
        )

    def test_meli_portal_url_prefers_pack_id_over_order_id(self):
        # Found in practice 2026-09-08: opening the link from a claim on a
        # pack order 404'd, because it was built with meli_order_id (the
        # individual order) instead of meli_pack_id (what the portal
        # actually keys the messaging thread by for a pack order).
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000035', resource_id='CLAIM-ORDER-FULL'),
                order_payload={'pack_id': '2000014407292449'},
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000035',
            )

        self.assertEqual(
            claim.meli_portal_url,
            'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
            'mensajeria/2000014407292449/reclamo/5000000035',
        )

    def test_import_claim_fetches_and_caches_reason_details(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000013', reason_id='PDD9939'),
                reason_payload={
                    'id': 'PDD9939', 'name': 'repentant_buyer',
                    'detail': 'Llegó lo que compré en buenas condiciones pero no lo quiero',
                },
            ),
        ) as mocked_get:
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000013',
            )

        self.assertEqual(claim.reason_code, 'PDD9939')
        self.assertEqual(claim.reason_name, 'repentant_buyer')
        self.assertIn('buenas condiciones', claim.reason_detail)
        calls_to_reasons = [
            call for call in mocked_get.call_args_list
            if '/claims/reasons/' in call.args[0]
        ]
        self.assertEqual(len(calls_to_reasons), 1)

        # A second claim with the SAME reason_code must reuse the cached
        # name/detail instead of calling the reasons endpoint again.
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(
                    claim_id='5000000014', resource_id='CLAIM-ORDER-FULL',
                    reason_id='PDD9939',
                ),
            ),
        ) as mocked_get_second:
            second_claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000014',
            )

        self.assertEqual(second_claim.reason_name, 'repentant_buyer')
        calls_to_reasons_second = [
            call for call in mocked_get_second.call_args_list
            if '/claims/reasons/' in call.args[0]
        ]
        self.assertEqual(len(calls_to_reasons_second), 0)

    def test_reason_lookup_failure_does_not_block_import(self):
        def fake_api_get(path, params=None, headers=None):
            if '/claims/reasons/' in path:
                raise requests.exceptions.RequestException("ML is down")
            if '/affects-reputation' in path:
                return {}
            return self._claim_payload(claim_id='5000000015', reason_id='PDD1111')

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000015',
            )

        self.assertTrue(claim)
        self.assertEqual(claim.reason_code, 'PDD1111')
        self.assertFalse(claim.reason_name)

    def test_import_claim_fetches_affects_reputation(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000016'),
                reputation_payload={
                    'affects_reputation': 'affected', 'has_incentive': True,
                    'due_date': '2026-09-10T12:00:00.000-04:00',
                },
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000016',
            )

        self.assertEqual(claim.affects_reputation, 'affected')
        self.assertTrue(claim.reputation_has_incentive)
        self.assertTrue(claim.reputation_due_date)

    def test_affects_reputation_failure_does_not_block_import(self):
        def fake_api_get(path, params=None, headers=None):
            if '/affects-reputation' in path:
                raise requests.exceptions.RequestException("ML is down")
            return self._claim_payload(claim_id='5000000017')

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000017',
            )

        self.assertTrue(claim)
        self.assertFalse(claim.affects_reputation)

    def test_affects_reputation_not_overwritten_on_transient_failure(self):
        with patch.object(
            type(self.config), '_api_get',
            side_effect=self._dispatch_api_get(
                self._claim_payload(claim_id='5000000018'),
                reputation_payload={'affects_reputation': 'affected', 'has_incentive': False},
            ),
        ):
            self.env['meli.claim']._meli_import_claim(self.test_company.id, '5000000018')

        def fake_api_get_failing(path, params=None, headers=None):
            if '/affects-reputation' in path:
                raise requests.exceptions.RequestException("ML is down")
            return self._claim_payload(claim_id='5000000018', stage='dispute')

        with patch.object(type(self.config), '_api_get', side_effect=fake_api_get_failing):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000018',
            )

        self.assertEqual(claim.affects_reputation, 'affected')

    def test_claim_with_no_matching_order_is_still_visible_under_the_company_rule(self):
        # Found in practice 2026-09-08: the ir.rule for meli.claim was
        # [('company_id', 'in', company_ids)] with no allowance for
        # company_id = False — since company_id is related to
        # sale_order_id.company_id, any claim that never found a
        # matching order (company_id blank) was invisible to EVERY
        # user in EVERY company, not just filtered out of one view.
        # Of 19 real claims in the shared database, 13 were hidden this
        # way. The rule must allow company_id = False through.
        with patch.object(
            type(self.config), '_api_get',
            return_value=self._claim_payload(
                claim_id='5000000019', resource_id='NO-MATCHING-ORDER-AT-ALL',
            ),
        ):
            claim = self.env['meli.claim']._meli_import_claim(
                self.test_company.id, '5000000019',
            )
        self.assertFalse(claim.sale_order_id)
        self.assertFalse(claim.company_id)

        meli_user = self.env['res.users'].create({
            'name': 'Meli Claim Viewer', 'login': 'meli_claim_company_rule_viewer',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('xe_meli_connector.group_meli_user').id,
            ])],
            'company_ids': [(6, 0, [self.test_company.id])],
            'company_id': self.test_company.id,
        })
        visible = self.env['meli.claim'].with_user(meli_user).search([
            ('claim_id', '=', '5000000019'),
        ])
        self.assertEqual(visible, claim)
