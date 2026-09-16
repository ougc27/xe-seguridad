import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        result = super()._action_done()
        for picking in self:
            order = picking.sale_id
            if order and order.meli_sync_source:
                # savepoint + broad except, same convention already used
                # twice in sale_order.py (_meli_auto_validate_full_pickings,
                # _meli_flag_status_change's call into
                # _meli_process_full_cancellation): a failure here must
                # never roll back super()._action_done() above — that's
                # the picking's own state transition, stock moves, and
                # quants, i.e. a warehouse worker's real physical delivery
                # confirmation. An unrelated invoicing/CFDI hiccup should
                # degrade to a manual-review log entry, not silently fail
                # (or corrupt) the transfer that already, genuinely,
                # happened.
                try:
                    with self.env.cr.savepoint():
                        order._meli_reconcile_invoicing()
                except Exception:
                    _logger.exception(
                        "Mercado Libre order %s: invoicing reconciliation "
                        "failed after transfer %s was validated — left "
                        "pending for manual review.",
                        order.client_order_ref, picking.name,
                    )
        return result
