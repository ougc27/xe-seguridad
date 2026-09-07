from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliSkuMapping(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product'].create({
            'name': 'Mesa de prueba',
        })
        cls.mapping = cls.env['meli.sku.mapping'].create({
            'product_id': cls.product.id,
            'meli_sku': 'ZTEST-MPTP01',
        })

    def test_find_product_by_meli_sku_match(self):
        found = self.env['meli.sku.mapping']._find_product_by_meli_sku('ZTEST-MPTP01')
        self.assertEqual(found, self.product)

    def test_find_product_by_meli_sku_no_match(self):
        found = self.env['meli.sku.mapping']._find_product_by_meli_sku('DOES-NOT-EXIST')
        self.assertFalse(found)

    def test_find_product_by_meli_sku_strips_whitespace(self):
        found = self.env['meli.sku.mapping']._find_product_by_meli_sku('  ZTEST-MPTP01  ')
        self.assertEqual(found, self.product)

    def test_find_product_by_meli_sku_empty_input(self):
        found = self.env['meli.sku.mapping']._find_product_by_meli_sku('')
        self.assertFalse(found)
        found = self.env['meli.sku.mapping']._find_product_by_meli_sku(False)
        self.assertFalse(found)

    def test_meli_sku_must_be_unique(self):
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['meli.sku.mapping'].create({
                    'product_id': self.product.id,
                    'meli_sku': 'ZTEST-MPTP01',
                })

    def test_same_product_can_have_multiple_skus(self):
        second = self.env['meli.sku.mapping'].create({
            'product_id': self.product.id,
            'meli_sku': 'ZTEST-MPTP-01',
        })
        self.assertEqual(second.product_id, self.mapping.product_id)

    def test_resolve_prefers_default_code_over_mapping_table(self):
        direct_product = self.env['product.product'].create({
            'name': 'Silla directa', 'default_code': 'ZTEST-SILLA-01',
        })
        found = self.env['meli.sku.mapping']._resolve_product_by_meli_sku('ZTEST-SILLA-01')
        self.assertEqual(found, direct_product)

    def test_resolve_falls_back_to_mapping_table(self):
        # 'ZTEST-MPTP01' has no product with that default_code — only the
        # mapping table row created in setUpClass resolves it.
        found = self.env['meli.sku.mapping']._resolve_product_by_meli_sku('ZTEST-MPTP01')
        self.assertEqual(found, self.product)

    def test_resolve_returns_empty_when_neither_matches(self):
        found = self.env['meli.sku.mapping']._resolve_product_by_meli_sku('NO-EXISTE-EN-NINGUN-LADO')
        self.assertFalse(found)

    def test_product_id_field_blocks_quick_create_in_form_view(self):
        # UI requirement: this view must not let users create a new product
        # from the product_id field — only pick an existing one.
        view = self.env.ref('xe_meli_connector.view_meli_sku_mapping_form')
        self.assertIn('no_create', view.arch)
        self.assertIn('no_create_edit', view.arch)
