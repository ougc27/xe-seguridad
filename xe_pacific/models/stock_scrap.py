from odoo import models, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools import float_is_zero


class StockScrap(models.Model):
    _inherit = 'stock.scrap'

    def action_validate(self):
        self.ensure_one()
        if float_is_zero(self.scrap_qty,
                            precision_rounding=self.product_uom_id.rounding):
            raise UserError(_('You can only enter positive quantities.'))

        if not self.check_available_qty():
            ctx = dict(self.env.context)
            ctx.update({
                'default_product_id': self.product_id.id,
                'default_location_id': self.location_id.id,
                'default_scrap_id': self.id,
                'default_quantity': self.product_uom_id._compute_quantity(self.scrap_qty, self.product_id.uom_id),
                'default_product_uom_name': self.product_id.uom_name
            })
            return {
                'name': self.product_id.display_name + _(': Insufficient Quantity To Scrap'),
                'view_mode': 'form',
                'res_model': 'stock.warn.insufficient.qty.scrap',
                'view_id': self.env.ref('stock.stock_warn_insufficient_qty_scrap_form_view').id,
                'type': 'ir.actions.act_window',
                'context': ctx,
                'target': 'new'
            }

        available_qty = self.check_available_stock_excluding_transit(self, False)

        if available_qty is False:
            return self.do_scrap()

        if available_qty - self.scrap_qty < 0:
            raise ValidationError(
                _(
                    "Insufficient Quantity To Scrap the {product_name} with lot {lot_name} "
                    "because the total available quantity is {qty}, but you are trying to remit "
                    "{requested_qty}, which exceeds the available stock."
                ).format(
                    product_name=self.product_id.display_name,
                    lot_name=self.lot_id.name,
                    qty=available_qty,
                    requested_qty=self.scrap_qty,
                )
            )
        return self.do_scrap()
