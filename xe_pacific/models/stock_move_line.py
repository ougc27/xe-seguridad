from odoo import api, models, _
from odoo.exceptions import UserError


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    def _apply_picking_lot_block(self):
        """If a lot is assigned on a purchase transfer flagged with
        block_lot_assignment, mark that lot as blocked."""
        for rec in self:
            if (rec.lot_id
                    and rec.picking_id.block_lot_assignment
                    and not rec.lot_id.blocked):
                rec.lot_id.blocked = True

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._apply_picking_lot_block()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if 'lot_id' in vals:
            self._apply_picking_lot_block()
        for rec in self:
            if rec.picking_id.state == 'done' and 'qty_done' in vals:
                raise UserError(_(
                    "You cannot modify quantities in moves of a completed picking."
                ))
        return res