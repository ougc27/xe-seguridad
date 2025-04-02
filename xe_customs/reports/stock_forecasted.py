# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, models, _
from odoo.exceptions import UserError


class StockForecasted(models.AbstractModel):
    _inherit = 'stock.forecasted_product_product'

    @api.model
    def action_unreserve_linked_picks(self, move_id):
        move = self.env['stock.move'].browse(move_id)
        if move.is_locked:
            raise UserError(_('You cannot unreserve a move that is locked.'))
        if move.picking_id.state == 'transit':
            raise UserError(_('You cannot unreserve a move with remission state.'))
        return super(StockForecasted, self).action_unreserve_linked_picks(move_id)
