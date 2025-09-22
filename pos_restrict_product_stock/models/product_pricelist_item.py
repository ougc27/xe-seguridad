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
