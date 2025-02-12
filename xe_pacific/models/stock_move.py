from odoo import models, api, _
from odoo.exceptions import UserError


class StockMove(models.Model):
    _inherit = 'stock.move'

    @api.depends('state', 'picking_id.is_locked')
    def _compute_is_initial_demand_editable(self):
        for move in self:
            #move.is_initial_demand_editable = not move.picking_id.is_locked or move.state == 'draft'
            move.is_initial_demand_editable = False

    def write(self, vals):
        for rec in self:
            new_quantity = vals.get("quantity", rec.quantity)
            if new_quantity != rec.quantity:
                if rec.picking_id.state == "transit":
                    raise UserError(_("You cannot change the demanded quantity in transit state"))
                if new_quantity > rec.move_id.product_uom_qty:
                    raise UserError(_("You cannot update the quantity with a quantity greater than the demand"))
        return super().write(vals)