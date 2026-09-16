from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestMeliConfigAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A dedicated test company, never `env.company` — this suite runs
        # against the team's real shared database, which may already have a
        # real meli.config for the real companies (company_id is unique).
        cls.test_company = cls.env['res.company'].with_context(
            disable_company_pricelist_creation=True
        ).create({'name': 'Test Co (meli access)'})
        cls.config = cls.env['meli.config'].create({
            'company_id': cls.test_company.id,
            'client_id': 'test-client-id',
            'client_secret': 'test-client-secret',
        })
        cls.internal_user = cls.env['res.users'].create({
            'name': 'Internal User',
            'login': 'meli_internal_user',
            'groups_id': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.sysadmin_user = cls.env['res.users'].create({
            'name': 'System Admin Without Meli Group',
            'login': 'meli_sysadmin_no_meli_group',
            'groups_id': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_system').id,
            ])],
        })

    def test_non_system_user_has_no_model_access(self):
        with self.assertRaises(AccessError):
            self.config.with_user(self.internal_user).read(['client_id'])

    def test_system_admin_without_meli_group_has_no_model_access(self):
        # 2026-08-28: found in practice that a real base.group_system admin
        # could still see/reach meli.config through the app-search and
        # command-palette launchers, because base.group_system was kept as
        # a safety net. Removed by explicit user decision — only
        # group_meli_admin should grant access, with no exception for
        # general system administrators.
        with self.assertRaises(AccessError):
            self.config.with_user(self.sysadmin_user).read(['client_id'])

    def test_secret_fields_restricted_to_meli_admin_only(self):
        # Deliberately excludes base.group_system (2026-08-28, user
        # decision): a general system administrator should not
        # automatically see these ML credentials just by being a system
        # admin for unrelated reasons — only group_meli_admin grants it.
        model_fields = self.env['meli.config']._fields
        for field_name in ('client_secret', 'access_token', 'refresh_token'):
            groups = model_fields[field_name].groups or ''
            self.assertIn('xe_meli_connector.group_meli_admin', groups)
            self.assertNotIn('base.group_system', groups)

    def test_client_id_also_restricted_to_meli_admin_only(self):
        # client_id isn't as sensitive as the secret/tokens, but there's no
        # legitimate reason for a non-admin to see it either.
        model_fields = self.env['meli.config']._fields
        groups = model_fields['client_id'].groups or ''
        self.assertIn('xe_meli_connector.group_meli_admin', groups)
        self.assertNotIn('base.group_system', groups)

    def test_secret_fields_not_tracked(self):
        model_fields = self.env['meli.config']._fields
        for field_name in ('client_secret', 'access_token', 'refresh_token'):
            self.assertFalse(
                getattr(model_fields[field_name], 'tracking', False),
                f"{field_name} must never have tracking enabled",
            )

    @mute_logger('odoo.sql_db')
    def test_unique_config_per_company(self):
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['meli.config'].create({
                    'company_id': self.config.company_id.id,
                    'client_id': 'other-client-id',
                })
