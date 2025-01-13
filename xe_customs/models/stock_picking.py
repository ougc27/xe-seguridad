# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models, _
from odoo.exceptions import UserError

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
        moves = []
        qty = sum(self.move_ids_without_package.mapped('quantity'))
        if qty == 0:
            self.action_assign()
            qty = sum(self.move_ids_without_package.mapped('quantity'))
            if qty == 0:
                raise UserError(_('There is no reserved stock to transit.'))
        for move in self.move_ids_without_package:

            if move.quantity > move.product_uom_qty:
                raise UserError(_('You cannot transit more than the ordered quantity.'))
            if move.product_id.detailed_type == 'product' and move.product_id.qty_available < move.quantity:
                raise UserError(_('Not enough quantity available for product %s.') % move.product_id.display_name)
            if move.quantity < move.product_uom_qty:
                moves.append(move)
        if len(moves) > 0:
            backorder = self.env['stock.picking'].create({
                'partner_id': self.partner_id.id,
                'picking_type_id': self.picking_type_id.id,
                'location_id': self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
                'origin': self.name,
                'supervisor_id': self.supervisor_id.id,
                'installer_id': self.installer_id.id,
                'scheduled_date': self.scheduled_date,
                'user_id': self.user_id.id,
                'is_locked': True,
                'backorder_id': self.id,
            })
            for move in moves:
                backorder.move_ids_without_package.create({
                    'name': move.name,
                    'product_id': move.product_id.id,
                    'product_uom_qty': move.product_uom_qty - move.quantity,
                    'product_uom': move.product_uom.id,
                    'picking_id': backorder.id,
                    'location_id': move.location_id.id,
                    'location_dest_id': move.location_dest_id.id,
                    'sale_line_id': move.sale_line_id.id,
                })
                move.write({
                    'product_uom_qty': move.quantity,
                })
                if move.quantity == 0:
                    move.unlink()

            backorder.write({
                'group_id': self.group_id.id,
            })
            backorder.action_assign()
            self.message_post(body=_('The backorder %s has been created.', backorder._get_html_link()))

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
