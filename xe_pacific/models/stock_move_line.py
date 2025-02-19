from odoo import models, _
from odoo.exceptions import UserError

import logging
_logger = logging.getLogger(__name__)


class StockMoveLine(models.Model):
    _inherit = 'stock.move.line'

    def write(self, vals):
        for rec in self:
            new_quantity = vals.get("quantity", rec.quantity)
            _logger.info("estoy en el write de stock.move.line")
            _logger.info(vals)
            _logger.info(new_quantity)
            _logger.info(rec.quantity)
            _logger.info("información padre del traslado")
            _logger.info(rec.picking_id)
            _logger.info(rec.move_id)
            if new_quantity != rec.quantity:
                if rec.picking_id.state == "transit":
                    raise UserError(_("You cannot change the demanded quantity in transit state"))
                if new_quantity > rec.move_id.product_uom_qty:
                    raise UserError(_("You cannot update the quantity with a quantity greater than the demand"))
        return super().write(vals)