from odoo import models, fields


class HelpdeskTicketOption(models.Model):
    _name = 'helpdesk.ticket.option'
    _description = 'Ticket Issue Option'
    _order = 'sequence, id'

    name = fields.Char(string='Option Name', required=True)
    sequence = fields.Integer(default=10)
    category_id = fields.Many2one(
        'helpdesk.ticket.category', 
        string='Category', 
        required=True, 
        ondelete='cascade'
    )
    active = fields.Boolean(default=True)
