from odoo import models, fields


class HelpdeskTicketCategory(models.Model):
    _name = 'helpdesk.ticket.category'
    _description = 'Ticket Issue Category'
    _order = 'sequence, id'

    name = fields.Char(string='Category Name', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
