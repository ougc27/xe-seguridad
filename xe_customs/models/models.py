from odoo import models


class BaseModel(models.AbstractModel):
    _inherit = 'base'

    def check_available_stock_excluding_transit(self, record, exclude_self=False):
        self.ensure_one()
        quants = self.env['stock.quant'].search([
            ('product_id', '=', record.product_id.id),
            ('lot_id', '=', record.lot_id.id),
            ('location_id', '=', record.location_id.id),
            ('company_id', '=', record.company_id.id),
            ('owner_id', '=', record.owner_id.id),
            ('package_id', '=', record.package_id.id)
        ])

        if not quants:
            return False

        quantity = sum(quants.mapped('available_quantity'))

        move_line_domain = [
            ('product_id', '=', record.product_id.id),
            ('lot_id', '=', record.lot_id.id),
            ('location_id', '=', record.location_id.id),
            ('picking_id.state', 'not in', ['cancel', 'done', 'transit']),
            ('company_id', '=', record.company_id.id),
            ('owner_id', '=', record.owner_id.id),
            ('package_id', '=', record.package_id.id)
        ]
        if exclude_self:
            move_line_domain.append(('id', '!=', record.id))

        not_transit_moves = self.env['stock.move.line'].search(move_line_domain)

        reserved_in_not_transit = sum(not_transit_moves.mapped('quantity'))

        available_qty = quantity + reserved_in_not_transit

        return available_qty
