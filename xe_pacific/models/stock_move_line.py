from odoo import models, _
from odoo.exceptions import UserError


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    def write(self, vals):
        #for rec in self:
            #new_quantity = vals.get("quantity", rec.quantity)
            #if new_quantity != rec.quantity:
                #if rec.picking_id.state == "transit":
                    #raise UserError(_("You cannot change the demanded quantity in transit state"))
                #if new_quantity > rec.move_id.product_uom_qty:
                    #raise UserError(_("You cannot update the quantity with a quantity greater than the demand"))
        return super().write(vals)