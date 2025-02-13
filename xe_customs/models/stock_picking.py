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
        
        qty = sum(self.move_ids_without_package.mapped('quantity'))
        if qty == 0:
            raise UserError(_('There is no reserved stock to transit.'))
    
        for move in self.move_ids_without_package:
            if move.has_tracking in ('lot', 'serial') and not move.lot_ids:
                raise UserError(_("The product %s requires a lot or serial number.") % move.product_id.display_name)
            if move.quantity > move.product_uom_qty:
                raise UserError(_('You cannot transit more than the ordered quantity.'))
            if move.product_id.detailed_type == 'product' and move.product_id.qty_available < move.quantity:
                raise UserError(_('Not enough quantity available for product %s.') % move.product_id.display_name)
    
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
