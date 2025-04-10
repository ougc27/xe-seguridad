from odoo import models, fields, api


class HelpdeskCall(models.Model):
    _name = 'helpdesk.call'
    _description = 'Ticket calls'

    name = fields.Char(string='Description')
    ticket_id = fields.Many2one('helpdesk.ticket', index=True, string='Ticket', readonly=True)
    tag_ids = fields.Many2many('helpdesk.tag', string='Tags')
    interaction_date = fields.Datetime(default=fields.Datetime.now)
    user_id = fields.Many2one('res.users',  default=lambda self: self.env.uid)
