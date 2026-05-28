from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HelpdeskTicketType(models.Model):
    _inherit = 'helpdesk.ticket.type'

    issue_category = fields.Selection([
        ('paintwork ', 'Paintwork'),
        ('functionality', 'Functionality'),
    ], copy=False)

    is_url_needed = fields.Boolean(string='Is URL needed?', copy=False)

    user_id = fields.Many2one('res.users', string='Responsible', copy=False)
