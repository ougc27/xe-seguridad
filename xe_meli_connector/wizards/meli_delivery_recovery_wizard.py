from odoo import _, models


class MeliDeliveryRecoveryWizard(models.TransientModel):
    _name = 'meli.delivery.recovery.wizard'
    _description = 'Mercado Libre Delivery Recovery'

    def action_run(self):
        """One-off healing for Full orders whose delivery was never
        generated/validated by the (now-fixed) automatic flow — see
        Task 2 (_meli_ensure_delivery) and Task 3
        (_meli_auto_validate_full_pickings) in
        docs/superpowers/plans/2026-09-09-meli-order-resilience.md for
        why these could get stuck before this fix. Scoped to this
        connector's own Full orders (meli_sync_source set,
        warehouse_id == some connection's own warehouse_fulfillment_id)
        — never touches a non-Mercado-Libre sale. Not a recurring
        automation: this is a manual, human-triggered tool that exists
        purely to heal records created before the fix, run once against
        the known-broken production backlog (~62 stuck Full pickings,
        ~48 Full orders with zero pickings from the 2026-08-28
        incident).
        """
        # Fix 4 (2026-09-09, final review — Important): scoped to
        # self.env.company.id on BOTH searches, matching the exact
        # pattern already established by this wizard's sibling recovery
        # wizards (meli.claim.recovery.wizard, meli.invoice.recovery.
        # wizard: `('company_id', '=', self.env.company.id)`). Before
        # this fix, action_run() had no company filter at all — a person
        # running it in one company's context could reach into and
        # irreversibly, inventory-movingly heal another company's own
        # orders.
        fulfillment_warehouse_ids = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.env.company.id),
            ('warehouse_fulfillment_id', '!=', False),
        ]).mapped('warehouse_fulfillment_id').ids
        orders = self.env['sale.order'].sudo().search([
            ('company_id', '=', self.env.company.id),
            ('meli_sync_source', '=', 'xe_meli_connector'),
            ('warehouse_id', 'in', fulfillment_warehouse_ids),
            ('state', '=', 'sale'),
        ])
        healed = 0
        for order in orders:
            live_pickings = order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
            missing = not order.picking_ids.filtered(lambda p: p.state != 'cancel')
            if missing or live_pickings:
                order._meli_ensure_delivery(True)
                healed += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Delivery recovery finished"),
                'message': _("%s order(s) processed.") % healed,
                'type': 'success',
                'sticky': False,
            },
        }
