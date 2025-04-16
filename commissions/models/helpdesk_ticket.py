from odoo import models, fields


class HelpdeskTicket(models.Model):
    _inherit = 'helpdesk.ticket'

    salesperson_id = fields.Many2one(
        'xe.agent',
        string='Salesperson',
        related='partner_id.agent1_id',
        readonly=True
    )
