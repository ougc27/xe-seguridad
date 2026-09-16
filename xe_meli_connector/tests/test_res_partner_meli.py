from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestResPartnerMeli(TransactionCase):

    def test_meli_buyer_id_field_exists_and_is_a_char(self):
        partner = self.env['res.partner'].create({
            'name': 'Meli Buyer Field Test', 'meli_buyer_id': '123456789',
        })
        self.assertEqual(partner.meli_buyer_id, '123456789')

    def _destination(self, **overrides):
        base = {
            'name': 'Juan Pérez', 'phone': '8112345678',
            'street': 'Calle Falsa 123', 'city': 'Monterrey',
            'zip': '64000', 'state_name': 'Nuevo León',
            'state_code': 'NLE', 'country_code': 'MX',
        }
        base.update(overrides)
        return base

    def test_returns_empty_recordset_without_buyer_id(self):
        contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '', self._destination(),
        )
        self.assertFalse(contact)

    def test_returns_empty_recordset_without_destination(self):
        contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', None,
        )
        self.assertFalse(contact)

    def test_creates_master_and_delivery_child_on_first_order(self):
        contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        self.assertEqual(contact.type, 'delivery')
        self.assertEqual(contact.name, 'Juan Pérez')
        self.assertEqual(contact.phone, '8112345678')
        self.assertEqual(contact.mobile, '8112345678')
        self.assertEqual(contact.street, 'Calle Falsa 123')
        self.assertEqual(contact.city, 'Monterrey')
        self.assertEqual(contact.zip, '64000')
        self.assertEqual(contact.country_id.code, 'MX')
        master = contact.parent_id
        self.assertEqual(master.meli_buyer_id, '555')
        self.assertEqual(master.name, 'Juan Pérez')

    def test_reuses_the_same_master_across_two_orders_for_the_same_buyer(self):
        first = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        second = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(name='Ana López', phone='8199999999'),
        )
        self.assertEqual(first.parent_id, second.parent_id)
        self.assertNotEqual(first, second)

    def test_master_name_is_never_changed_after_creation(self):
        first = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        master_before = first.parent_id.name
        self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(name='Ana López'),
        )
        self.assertEqual(first.parent_id.name, master_before)

    def test_reuses_an_identical_existing_delivery_child(self):
        first = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        second = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        self.assertEqual(first, second)

    def test_creates_a_new_child_when_any_field_differs_never_edits_the_old_one(self):
        first = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(),
        )
        first_values_before = (first.name, first.phone, first.street)
        second = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(street='Otra Calle 456'),
        )
        self.assertNotEqual(first, second)
        self.assertEqual(
            (first.name, first.phone, first.street), first_values_before,
        )

    def test_unmatched_state_name_leaves_state_id_blank_instead_of_guessing(self):
        contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(
                state_name='Nowhere That Exists', state_code='ZZZ',
            ),
        )
        self.assertFalse(contact.state_id)
        self.assertEqual(contact.country_id.code, 'MX')

    def test_state_name_match_is_exact_not_a_substring(self):
        # Real data in this database (base MX seed data): "México",
        # "Ciudad de México" and "Estado de México" all contain "México"
        # as a substring. A plain 'ilike' match on state_name="México"
        # would hit all three and silently pick whichever sorts first by
        # code — exactly the "silently wrong state" outcome
        # _meli_resolve_state exists to avoid. '=ilike' (exact,
        # case-insensitive) must resolve to the one literal match only.
        contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            '555', self._destination(state_name='México', state_code='ZZZ-NOMATCH'),
        )
        self.assertEqual(contact.state_id.name, 'México')
