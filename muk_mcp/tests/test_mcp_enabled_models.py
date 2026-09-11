from odoo.exceptions import ValidationError
from odoo.tests import common


class TestMcpEnabledModel(common.TransactionCase):

    # ----------------------------------------------------------
    # Setup
    # ----------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enabled_model = cls.env['mcp.enabled.model']
        cls.res_partner_category = cls.env['ir.model']._get('res.partner.category')

    # ----------------------------------------------------------
    # Tests: blocklist semantics (no record = allowed)
    # ----------------------------------------------------------

    def test_no_record_means_model_is_allowed(self):
        self.assertTrue(
            self.enabled_model.is_model_enabled('res.partner.category')
        )

    def test_no_record_means_read_create_write_allowed(self):
        for operation in ('read', 'create', 'write'):
            self.assertTrue(
                self.enabled_model.check_model_operation_enabled(
                    'res.partner.category', operation,
                )
            )

    def test_no_record_means_unlink_is_blocked(self):
        # Delete is the one exception: it's closed by default, unlike
        # read/create/write, because MCP clients must never be able
        # to delete records unless a model is explicitly opted in.
        self.assertFalse(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'unlink',
            )
        )

    # ----------------------------------------------------------
    # Tests: an active record narrows to its own allow_* flags
    # ----------------------------------------------------------

    def test_active_record_enforces_its_own_flags(self):
        self.enabled_model.create({
            'model_id': self.res_partner_category.id,
            'allow_read': True,
            'allow_create': False,
            'allow_write': False,
            'allow_unlink': False,
        })
        self.assertTrue(
            self.enabled_model.is_model_enabled('res.partner.category')
        )
        self.assertTrue(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'read',
            )
        )
        self.assertFalse(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'write',
            )
        )

    def test_active_record_with_allow_unlink_true_allows_delete(self):
        self.enabled_model.create({
            'model_id': self.res_partner_category.id,
            'allow_unlink': True,
        })
        self.assertTrue(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'unlink',
            )
        )

    def test_active_record_with_allow_unlink_false_blocks_delete(self):
        self.enabled_model.create({
            'model_id': self.res_partner_category.id,
            'allow_unlink': False,
        })
        self.assertFalse(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'unlink',
            )
        )

    # ----------------------------------------------------------
    # Tests: a deactivated record fully blocks the model
    # ----------------------------------------------------------

    def test_inactive_record_blocks_the_model_entirely(self):
        self.enabled_model.create({
            'model_id': self.res_partner_category.id,
            'active': False,
            'allow_read': True,
        })
        self.assertFalse(
            self.enabled_model.is_model_enabled('res.partner.category')
        )
        self.assertFalse(
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'read',
            )
        )

    # ----------------------------------------------------------
    # Tests: validation
    # ----------------------------------------------------------

    def test_invalid_operation_raises(self):
        with self.assertRaises(ValidationError):
            self.enabled_model.check_model_operation_enabled(
                'res.partner.category', 'bogus',
            )
