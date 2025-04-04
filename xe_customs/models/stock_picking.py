# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models, _
from odoo.exceptions import UserError, ValidationError

import logging
_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    supervisor_id = fields.Many2one('supervisor.installer', 'Supervisor', help="Define the supervisor of the picking.")
    installer_id = fields.Many2one('supervisor.installer', 'Installer', help="Define the installer of the picking.")
    state = fields.Selection(selection_add=[('transit', 'Transit'), ('done',)])
    remission_date = fields.Datetime(
        'Remission Date', copy=False, readonly=True)

    def action_transit(self):
        self.ensure_one()
        if self.state == 'done':
            raise UserError(_('You cannot remission a validated order'))
        
        qty = sum(self.move_ids_without_package.mapped('quantity'))
        if qty == 0:
            raise UserError(_('There is no reserved stock to transit.'))
    
        for move in self.move_ids_without_package:
            if move.quantity > 0:
                if move.has_tracking in ('lot', 'serial') and not move.lot_ids:
                    raise UserError(_("The product %s requires a lot or serial number.") % move.product_id.display_name)
            if move.quantity > move.product_uom_qty:
                raise UserError(_('You cannot transit more than the ordered quantity.'))
            if move.product_id.detailed_type == 'product' and move.product_id.qty_available < move.quantity:
                raise UserError(_('Not enough quantity available for product %s.') % move.product_id.display_name)
        for move_line in self.move_line_ids:
            restrict_remission_with_no_stock = self.env['ir.config_parameter'].sudo().get_param(
                'restrict_remission_with_no_stock', None)
            if restrict_remission_with_no_stock == "1":
                if move_line.quantity > 0 and move_line.lot_id:
                    quantity = self.env['stock.quant'].with_context(allow_negative=True)._get_available_quantity(move_line.product_id, move_line.location_id, lot_id=move_line.lot_id, package_id=move_line.package_id, owner_id=move_line.owner_id, strict=False)
                    _logger.info("esto retorna el _get_available_quantity")
                    _logger.info(quantity)
                    not_transit_moves = self.env['stock.move.line'].search([
                        ('product_id', '=', move_line.product_id.id),
                        ('lot_id', '=', move_line.lot_id.id),
                        ('location_id', '=', move_line.location_id.id),
                        ('picking_id.state', 'not in', ['cancel', 'done', 'transit']),
                        ('id', '!=', move_line.id)
                    ])             
                    _logger.info("estos son los not_transit_moves")
                    _logger.info(not_transit_moves)
                    reserved_in_not_transit = sum(not_transit_moves.mapped('quantity'))
                    available_qty = quantity + reserved_in_not_transit
                    _logger.info("este es el available quantity")
                    _logger.info(available_qty)
                    if available_qty < 0:
                        pickings_names = ", ".join(not_transit_moves.mapped("picking_id.name"))
                        raise ValidationError(
                            _(
                                "You cannot reserve the product '{product_name}' because the total "
                                "available quantity is {qty}, but you are trying to remit "
                                "{requested_qty}, which exceeds the available stock."
                            ).format(
                                product_name=move_line.product_id.display_name,
                                qty=available_qty,
                                requested_qty=move_line.quantity,
                            )
                        )
        self.write({
            'state': 'transit',
            'is_locked': True,
            'remission_date': fields.Datetime.now()
        })

    def do_unreserve(self):
        self.ensure_one()
        if self.state == 'transit':
            raise UserError(_('You cannot unreserve a transit picking.'))
        return super(StockPicking, self).do_unreserve()

    def action_cancel_transit(self):
        self.ensure_one()
        self.write({
            'state': 'confirmed',
            'is_locked': False,
        })
        self.move_ids._do_unreserve()

    def button_validate_remission(self):
        if self.state != 'transit':
            raise UserError(_('You cannot validate a delivery order unless it is in the "Remission" state.'))
        return super(StockPicking, self).button_validate()
