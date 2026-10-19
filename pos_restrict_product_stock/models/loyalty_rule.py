from odoo import fields, models

class LoyaltyRule(models.Model):
    _inherit = 'loyalty.rule'

    maximum_qty = fields.Integer(
        'Maximum Quantity',
        default=1,
        help="Maximum quantity of the order line the coupon can be applied "
            "to. Set 0 for no limit.",
    )
