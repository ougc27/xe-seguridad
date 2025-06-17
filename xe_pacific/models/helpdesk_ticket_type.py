from odoo import models, fields, api, _
from odoo.exceptions import UserError


class HelpdeskTicketType(models.Model):
    _inherit = 'helpdesk.ticket.type'

    issue_category = fields.Selection([
        ('paintwork ', 'Paintwork'),
        ('functionality', 'Functionality'),
    ], copy=False)
