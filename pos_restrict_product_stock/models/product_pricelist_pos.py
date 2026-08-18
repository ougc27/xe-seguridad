from odoo import fields, models


class ProductPricelist(models.Model):
    _inherit = "product.pricelist"

    pos_price_included = fields.Boolean(
        string="POS Price Tax Included",
        help="If enabled, the rules of this pricelist use the 'POS Price (Tax "
             "Included)' field. In the POS, when the point of sale tax is set as "
             "'Included in Price', that price is used instead of the normal one. "
             "The POS price column is only shown when this box is checked.",
    )
