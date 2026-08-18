from odoo import models, fields
from odoo.addons import decimal_precision as dp

class PricelistItem(models.Model):
    _inherit = "product.pricelist.item"

    price_discount = fields.Float(
        string="Price Discount",
        default=0,
        digits=dp.get_precision('Product Price'),
        help="You can apply a mark-up by setting a negative discount."
    )

    pos_price_incl = fields.Float(
        string="POS Price (Tax Included)",
        default=0,
        digits=dp.get_precision('Product Price'),
        help="Tax-included price used by the POS when the point of sale tax is "
             "set as 'Included in Price'. Usually the fixed price x 1.16 "
             "rounded to the nearest whole unit."
    )
