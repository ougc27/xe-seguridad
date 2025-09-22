from odoo import api, fields, models, _


class ProductPromotionLocation(models.Model):
    _name = 'product.promotion.location'
    _description = 'Product Promotion By Location'

    product_id = fields.Many2one('product.product', required=True)

    warehouse_id = fields.Many2many('stock.warehouse', string='Warehouse')

    pricelist_id = fields.Many2one('product.pricelist', string='Pricelist', required=True)

    date_start = fields.Datetime(
        string="Start Date",
        help="Starting datetime for the product promition location validation\n"
            "The displayed value depends on the timezone set in your preferences.")
    date_end = fields.Datetime(
        string="End Date",
        help="Ending datetime for the product promition location validation\n"
            "The displayed value depends on the timezone set in your preferences.")
    
    price_type = fields.Selection([
        ('fixed', 'Fixed'),
        ('variable', 'Variable')
    ], string="Price Type", default='fixed', required=True,
    help="Determines if the price is fixed or variable. If fixed, the monthly payment scheme cannot be enabled.")

    price = fields.Float(digits='Product Price', compute="_compute_price", readonly=True, store=True)

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
    )

    @api.depends('pricelist_id', 'product_id')
    def _compute_price(self):
        for rec in self:
            if rec.pricelist_id and rec.product_id:
                pricelist_context = dict(self.env.context, date=fields.Date.today())
                price = rec.pricelist_id._get_product_price(
                    rec.product_id.with_context(pricelist_context),
                    1.0,
                    rec.product_id.env.user.partner_id
                )
                if price:
                    rec.price = price
                    continue
            rec.price = 0
