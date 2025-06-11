from odoo import models, fields, _
from odoo.exceptions import UserError


class TicketTransferWizard(models.TransientModel):
    _name = 'generate.ticket.wizard'
    _description = 'Transfer Wizard desde Helpdesk'

    picking_id = fields.Many2one('stock.picking', readonly=True)
    user_id = fields.Many2one('res.users', readonly=True)
    warehouse_id = fields.Many2one('stock.warehouse', readonly=True)
    partner_id = fields.Many2one('res.partner', readonly=True)
    ticket_type = fields.Selection([
        ('paint', 'Paint'),
        ('functionality', 'Functionality')], required=True)
    team_id = fields.Many2one('helpdesk.team', string='Helpdesk Team', domain="[('company_id', '=', company_id)]", required=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    def action_validate_ticket(self):
        picking_id = self.picking_id
        ticket_type_name = 'pintura' if self.ticket_type == 'paint' else 'funcionamiento'
        ticket_type_id = self.env['helpdesk.ticket.type'].search([('name', 'ilike', ticket_type_name)]).id
        ticket_vals = {
            'picking_id': picking_id.id,
            'team_id': self.team_id.id,
            'ticket_type_id': ticket_type_id,
            'user_id': self.user_id.id,
            'partner_id': self.partner_id.id,
        }

        ticket = self.env['helpdesk.ticket'].create(ticket_vals)

        ticket.autofill_from_picking(picking_id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'helpdesk.ticket',
            'res_id': ticket.id,
            'view_mode': 'form',
            'target': 'current',
        }
