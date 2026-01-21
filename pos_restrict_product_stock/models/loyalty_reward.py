from odoo import models, fields


class LoyaltyReward(models.Model):
    _inherit = "loyalty.reward"

    discount_application = fields.Selection(
        [
            ("line", "Apply to Line Total"),
            ("unit", "Apply Per Unit"),
        ],
        string="Discount Application",
        default="line",
        help=(
            "Defines whether the discount is applied to the total amount of "
            "the order line or individually to each unit."
        ),
    )
