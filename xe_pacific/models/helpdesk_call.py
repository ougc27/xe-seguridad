from odoo import models, fields, api


class HelpdeskCall(models.Model):
    _name = 'helpdesk.call'
    _description = 'Ticket calls'

    name = fields.Char(string='Description')
    ticket_id = fields.Many2one('helpdesk.ticket', index=True, string='Ticket', readonly=True)
    tag_id = fields.Many2one('helpdesk.tag', index=True, string='Tag')
    date_call = fields.Datetime(string='Fecha', default=fields.Datetime.now)
