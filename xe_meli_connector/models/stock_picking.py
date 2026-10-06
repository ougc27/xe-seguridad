from odoo import models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        result = super()._action_done()
        for picking in self:
            order = picking.sale_id
            if order and order.meli_sync_source:
                # Fix 2026-10-05 (user-caught: validating a transfer felt
                # slow): this used to call order._meli_reconcile_invoicing()
                # and order._meli_close_pack_if_every_sibling_cancelled()
                # inline, synchronously — both can make several live
                # Mercado Libre API calls, blocking the picking's own
                # HTTP response until every one of them finished. Queued
                # instead — see sale.order._meli_reconcile_after_picking_
                # validated's own docstring for the full rationale.
                order.with_delay(
                    priority=0, channel='root.meli_sales', max_retries=8,
                    description=(
                        f"Reconcile Mercado Libre invoicing after "
                        f"transfer {picking.name}"
                    ),
                )._meli_reconcile_after_picking_validated(picking.name)
        return result
