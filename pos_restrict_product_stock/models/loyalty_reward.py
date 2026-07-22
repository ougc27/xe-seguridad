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

    def _get_discount_product_values(self):
        """Ensure the auto-generated discount product always has a product
        category. Core does not set categ_id and relies on the default, which
        can resolve to an empty value in this database, creating products
        without a category that later break the POS product loading.
        """
        values = super()._get_discount_product_values()
        default_category = self.env.ref(
            "product.product_category_all", raise_if_not_found=False
        ) or self.env["product.category"].search([], limit=1)
        for vals in values:
            if not vals.get("categ_id") and default_category:
                vals["categ_id"] = default_category.id
        return values
