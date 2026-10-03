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
                # Fix 2026-10-02 (user-caught, real production case
                # S972773): a Full pack sibling's own physical return
                # can genuinely complete LATER than the cancellation
                # notification that originally tried to close the pack
                # — a human confirming it by hand (the "Confirm
                # Physical Return" wizard), or any of this module's own
                # catch-up scripts, validates a transfer here well
                # after _meli_close_pack_if_every_sibling_cancelled's
                # own one-shot attempt already gave up for lack of a
                # physical return. Nothing else ever revisits that
                # check once it fails once — confirmed live: every
                # condition it checks (qty_delivered netted to 0, the
                # credit note's own sale_line_ids covering this line)
                # was already true, yet the sale stayed 'sale' forever
                # because nothing asked again. Re-checked here, every
                # time ANY transfer on a Full pack order validates —
                # already a cheap no-op (returns immediately) unless
                # every sibling genuinely qualifies right now.
                if order.state != 'cancel' and order.meli_pack_id:
                    try:
                        with self.env.cr.savepoint():
                            order._meli_close_pack_if_every_sibling_cancelled()
                    except Exception:
                        _logger.exception(
                            "Mercado Libre order %s: re-checking whether "
                            "the whole pack could now close failed after "
                            "transfer %s was validated — left pending for "
                            "manual review.",
                            order.client_order_ref, picking.name,
                        )
        return result
