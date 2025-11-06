from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SplitPickingWizard(models.Model):
    _name = 'split.picking.wizard'
    _description = 'Wizard to split picking'

    picking_id = fields.Many2one('stock.picking', string='Picking', required=True)
    line_ids = fields.One2many('split.picking.wizard.line', 'wizard_id', string='Lines')

    def action_confirm(self):
        """Split the original picking into a new partial transfer."""
        self.ensure_one()
        picking = self.picking_id
        if not picking:
            raise UserError(_("There is no picking associated."))

        if picking.state not in ('assigned', 'confirmed', 'cancel'):
            raise UserError(_("Only confirmed or assigned pickings can be split."))

        StockMove = self.env['stock.move']
        removed_moves_data = []

        for line in self.line_ids:
            move = line.move_id

            # Basic validations
            if line.qty_to_split < 0:
                raise UserError(_("You cannot enter negative quantities for %s.") % line.product_id.display_name)
            if line.qty_to_split > line.qty_available:
                raise UserError(_("The quantity to split for %s cannot exceed the available quantity (%s).") %
                                (line.product_id.display_name, line.qty_available))
            if line.qty_to_split == 0:
                continue

            qty_to_remove = min(line.qty_to_split, move.product_uom_qty)

            removed_moves_data.append({
                'product_id': move.product_id.id,
                'product_uom': move.product_uom.id,
                'product_uom_qty': qty_to_remove,
                'name': move.name,
                'location_id': move.location_id.id,
                'location_dest_id': move.location_dest_id.id,
                'sale_line_id': move.sale_line_id.id if move.sale_line_id else False,
                'description_picking': move.product_id.name,
                'group_id': move.group_id.id,
                'purchase_line_id': move.purchase_line_id.id if move.purchase_line_id else False,
            })

            # Subtract or remove the original move
            if move.product_uom_qty == qty_to_remove:
                move.unlink()
            else:
                move.write({'product_uom_qty': move.product_uom_qty - qty_to_remove})

        if not removed_moves_data:
            raise UserError(_("No valid quantities selected to split."))

        # Create the new picking
        new_picking_vals = {
            'picking_type_id': picking.picking_type_id.id,
            'partner_id': picking.partner_id.id,
            'location_id': picking.location_id.id,
            'location_dest_id': picking.location_dest_id.id,
            'origin': picking.origin,
            'company_id': picking.company_id.id,
            'scheduled_date': picking.scheduled_date,
            'group_id': picking.group_id.id if picking.group_id else False,
            'note': picking.note,
            'sale_id': picking.sale_id.id if picking.sale_id else False,
            'user_id': picking.user_id.id,
        }

        new_picking = self.env['stock.picking'].create(new_picking_vals)

        # Create the new moves in the new picking
        for data in removed_moves_data:
            data.update({'picking_id': new_picking.id})
            StockMove.create(data)

        # Confirm and unreserve
        new_picking.action_confirm()
        new_picking.do_unreserve()

        # Post a message on the original picking
        self.env['mail.message'].create({
            'model': 'stock.picking',
            'res_id': picking.id,
            'body': _('A new partial transfer has been created: %s') % new_picking.name,
            'message_type': 'comment',
            'subtype_id': self.env.ref('mail.mt_note').id,
            'author_id': self.env.user.partner_id.id,
        })

        # Open the new picking
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Picking'),
            'res_model': 'stock.picking',
            'view_mode': 'form',
            'res_id': new_picking.id,
            'target': 'current',
        }


class SplitPickingWizardLine(models.Model):
    _name = 'split.picking.wizard.line'
    _description = 'Lines of the split picking wizard'

    wizard_id = fields.Many2one('split.picking.wizard', string='Wizard', ondelete='cascade')
    move_id = fields.Many2one('stock.move', string='Original Move')
    product_id = fields.Many2one('product.product', string='Product')
    description = fields.Char(string='Description')
    qty_available = fields.Float(string='Available Quantity', digits='Product Unit of Measure')
    qty_to_split = fields.Float(string='Quantity to Split', digits='Product Unit of Measure')

    @api.onchange('qty_to_split')
    def _onchange_qty_to_split(self):
        for line in self:
            if line.qty_to_split < 0:
                line.qty_to_split = 0
                return {
                    'warning': {
                        'title': _('Invalid Quantity'),
                        'message': _('You cannot enter a negative quantity.'),
                    }
                }
            if line.qty_to_split > line.qty_available:
                line.qty_to_split = 0
                return {
                    'warning': {
                        'title': _('Exceeded Quantity'),
                        'message': _('You cannot enter more than the available quantity.'),
                    }
                }
