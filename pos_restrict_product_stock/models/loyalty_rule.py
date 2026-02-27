from odoo import fields, models

class LoyaltyRule(models.Model):
    _inherit = 'loyalty.rule'

    maximum_qty = fields.Integer('Maximum Quantity', default=1)
