from odoo import fields, models


class CrmTeam(models.Model):
    _inherit = 'crm.team'

    terms_and_conditions = fields.Html()
