from odoo import fields, models, api, _
from odoo.exceptions import UserError

import logging
_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _default_group_id(self):
        if self.env.context.get('default_picking_id'):
            return self.env['stock.picking'].browse(self.env.context['default_picking_id']).group_id.id
        return False

    group_id = fields.Many2one('procurement.group',
        'Procurement Group',
        default=_default_group_id, 
        index=True,
        tracking=True)

    @api.depends('state', 'picking_id.is_locked')
    def _compute_is_initial_demand_editable(self):
        for move in self:
            if move.picking_id.picking_type_code == 'outgoing':
                move.is_initial_demand_editable = False
                continue
            move.is_initial_demand_editable = not move.picking_id.is_locked or move.state == 'draft'

    def write(self, vals):
        for rec in self:
            new_quantity = vals.get("quantity", rec.quantity)
            if new_quantity != rec.quantity:
                _logger.info("este es el write de stock_move")
                _logger.info(new_quantity)
                _logger.info(rec.quantity)
                if rec.picking_id.state == "transit":
                    raise UserError(_("You cannot change the demanded quantity in transit state"))
                if new_quantity > rec.product_uom_qty:
                    raise UserError(_("You cannot update the quantity with a quantity greater than the demand"))
        return super().write(vals)
