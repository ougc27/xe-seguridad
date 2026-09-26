from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMeliSecurityGroups(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli security groups)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
        })
        cls.product = cls.env['product.product'].create({'name': 'Producto de prueba'})

        cls.group_user = cls.env.ref('xe_meli_connector.group_meli_user')
        cls.group_advanced = cls.env.ref('xe_meli_connector.group_meli_advanced')
        cls.group_admin = cls.env.ref('xe_meli_connector.group_meli_admin')

        # meli.config also carries a global multi-company ir.rule
        # (company_id in company_ids) — every test user needs test_company
        # in their allowed companies, or the rule blocks them regardless of
        # which group they hold.
        company_vals = {
            'company_ids': [(6, 0, [cls.test_company.id])],
            'company_id': cls.test_company.id,
        }
        cls.user_basic = cls.env['res.users'].create({
            'name': 'Meli User Basic', 'login': 'meli_group_user',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id, cls.group_user.id])],
            **company_vals,
        })
        cls.user_advanced = cls.env['res.users'].create({
            'name': 'Meli User Advanced', 'login': 'meli_group_advanced',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id, cls.group_advanced.id])],
            **company_vals,
        })
        cls.user_admin = cls.env['res.users'].create({
            'name': 'Meli User Admin', 'login': 'meli_group_admin',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id, cls.group_admin.id])],
            **company_vals,
        })

    def test_group_hierarchy_implies_downward(self):
        self.assertTrue(self.user_admin.has_group('xe_meli_connector.group_meli_advanced'))
        self.assertTrue(self.user_admin.has_group('xe_meli_connector.group_meli_user'))
        self.assertTrue(self.user_advanced.has_group('xe_meli_connector.group_meli_user'))
        self.assertFalse(self.user_advanced.has_group('xe_meli_connector.group_meli_admin'))
        self.assertFalse(self.user_basic.has_group('xe_meli_connector.group_meli_advanced'))

    def test_basic_user_cannot_access_config_or_sku_mapping(self):
        with self.assertRaises(AccessError):
            self.config.with_user(self.user_basic).read(['company_id'])
        with self.assertRaises(AccessError):
            self.env['meli.sku.mapping'].with_user(self.user_basic).search([])

    def test_advanced_user_cannot_access_config_or_sku_mapping(self):
        # Fix 2026-09-25 (user decision): SKU Mapping moved from
        # group_meli_advanced to group_meli_admin-only, same as
        # Configuración/Import Order/the Excel invoice wizards — an
        # "Advanced" user is no longer enough for either.
        with self.assertRaises(AccessError):
            self.env['meli.sku.mapping'].with_user(self.user_advanced).create({
                'product_id': self.product.id, 'meli_sku': 'ADV-SKU-01',
            })
        with self.assertRaises(AccessError):
            self.config.with_user(self.user_advanced).read(['company_id'])

    def test_admin_user_can_manage_sku_mapping(self):
        mapping = self.env['meli.sku.mapping'].with_user(self.user_admin).create({
            'product_id': self.product.id, 'meli_sku': 'ADMIN-SKU-01',
        })
        self.assertTrue(mapping)

    def test_basic_user_can_refresh_invoice_document_status(self):
        # Found in practice 2026-09-08: meli.invoice.document was left
        # read-only (write=0) for group_meli_user from when the model
        # was view-only — the "Actualizar Status" button writes to
        # `status`, so a real user hit AccessError clicking it.
        document = self.env['meli.invoice.document'].create({
            'meli_order_id': 'SEC-TEST-0001', 'transaction_type': 'sale',
        })
        document.with_user(self.user_basic).write({'status': 'authorized'})
        self.assertEqual(document.status, 'authorized')

    def test_actions_declare_the_same_groups_as_their_menus(self):
        # groups= on a <menuitem> only hides the sidebar entry — it does
        # NOT stop the app-search launcher (or a direct action call) from
        # opening the window action for an unauthorized user. groups_id on
        # the ir.actions.act_window record itself is what actually blocks
        # that. Found in practice 2026-08-28: a non-admin could still find
        # "Configuración" via the home screen search.
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_config').groups_id.ids),
            {self.group_admin.id},
        )
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_sku_mapping').groups_id.ids),
            {self.group_admin.id},
        )
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_orders').groups_id.ids),
            {self.group_user.id},
        )
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_order_import_wizard').groups_id.ids),
            {self.group_admin.id},
        )
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_invoice_import_batch_wizard').groups_id.ids),
            {self.group_admin.id},
        )
        self.assertEqual(
            set(self.env.ref('xe_meli_connector.action_meli_invoice_import_batches').groups_id.ids),
            {self.group_admin.id},
        )

    def test_admin_user_can_access_config_including_sensitive_fields(self):
        # Model-level access (ir.model.access.csv) AND field-level groups=
        # (client_id/client_secret/tokens) both need to admit
        # group_meli_admin on their own — there's no base.group_system
        # fallback (removed 2026-08-28, by user decision).
        values = self.config.with_user(self.user_admin).read(['company_id', 'client_id'])
        self.assertTrue(values)
        self.assertEqual(values[0]['client_id'], 'test-client-id')
