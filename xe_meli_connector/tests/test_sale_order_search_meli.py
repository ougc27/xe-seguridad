from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSaleOrderSearchMeli(TransactionCase):
    """The free-text 'Order' search box on sale.order is shared by both
    Ventas and Mercado Libre > Pedidos (action_open_meli_orders never
    sets its own search_view_id) — found in practice 2026-09-08: neither
    meli_order_id nor meli_pack_id was searchable there even though both
    are stored as their own fields, so a person pasting either id from
    Mercado Libre's own portal couldn't find the matching sale order.
    """

    def test_free_text_search_domain_covers_meli_order_id_and_pack_id(self):
        view = self.env.ref('xe_meli_connector.view_sales_order_filter_meli')
        arch = self.env['sale.order'].get_view(view_id=view.id, view_type='search')['arch']
        self.assertIn('meli_order_id', arch)
        self.assertIn('meli_pack_id', arch)

    def test_free_text_search_still_covers_the_original_fields(self):
        # Regression guard: the fix replaces the whole filter_domain
        # attribute, so it's easy to accidentally drop what the base
        # `sale` module already searched on (name/client_order_ref/partner).
        view = self.env.ref('xe_meli_connector.view_sales_order_filter_meli')
        arch = self.env['sale.order'].get_view(view_id=view.id, view_type='search')['arch']
        self.assertIn('client_order_ref', arch)
        self.assertIn('partner_id', arch)

    def test_tree_view_exposes_meli_last_status_hidden_by_default(self):
        # Shared by Ventas and Mercado Libre > Pedidos, same as the search
        # view above — hidden by default (optional="hide") so it doesn't
        # change what a non-Mercado-Libre user sees out of the box; a
        # person can still turn it on from the column picker to spot
        # which orders changed status and need a look.
        view = self.env.ref('xe_meli_connector.view_order_tree_meli')
        arch = self.env['sale.order'].get_view(view_id=view.id, view_type='tree')['arch']
        self.assertIn('meli_last_status', arch)
        self.assertIn('optional="hide"', arch)
