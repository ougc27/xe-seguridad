from collections import defaultdict
from odoo import models, api, fields, Command, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    is_exception = fields.Boolean(string="Excepción")

    pos_store = fields.Many2one('res.partner', domain="[('is_pos_store', '=', True)]")

    def write(self, vals):
        if 'order_line' in vals:
            for order in self:
                if order.picking_ids.filtered(lambda p: p.state == 'transit'): 
                    raise UserError(_("You cannot modify sale order lines because there is at least one transfer in remission status."))
        return super(SaleOrder, self).write(vals)

    def action_unlock(self):
        for rec in self:
            if rec.picking_ids.filtered(lambda picking: picking.state == 'transit'):
                raise UserError(_('An order cannot be unlocked if it has a delivery in remission status.'))
            rec.locked = False

    def action_cancel(self):
        for rec in self:
            delivered = rec.order_line.filtered(lambda line: line.qty_delivered > 0)
            transit_pickings = rec.picking_ids.filtered(lambda picking: picking.state == 'transit')
            if delivered or transit_pickings:
                raise UserError(_('It contains a delivered product or deliveries with the status "remission".'))
        return super().action_cancel()

    def _action_confirm(self):
        result = super()._action_confirm()
        for rec in self:
            separate_remissions = self.env['ir.config_parameter'].sudo().get_param(
                'separate_remissions_activate', None)
            if rec.company_id.id == 4 and separate_remissions == "1":
                no_separate = rec.order_line.filtered(
                    lambda record: record.product_id.default_code in ['FLTLOCAL', 'INSFOR'])
                if no_separate:
                    continue
                if rec.team_id.name.lower() == 'constructoras':
                    rec.picking_ids.separate_construction_remissions()
                else:
                    if rec.picking_ids[0] and rec.picking_ids[0].shipping_assignment == 'logistics':
                        rec.picking_ids.separate_client_remissions()
            for picking in rec.picking_ids:
                moves = picking.move_ids.filtered(lambda move: (
                     move.product_id.product_tmpl_id.not_automatic_lot_number
                ))
                if moves:
                    picking.do_unreserve()
                    picking.action_assign()
        return result
