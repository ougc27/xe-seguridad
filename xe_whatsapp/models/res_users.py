from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    is_available = fields.Boolean(string="If true the system assign conversations to the user", default=False)
