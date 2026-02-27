from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class ProductPromotionLocation(models.Model):
    _name = 'product.promotion.location'
    _description = 'Product Promotion By Location'

    product_id = fields.Many2one('product.product', required=True)
    warehouse_ids = fields.Many2many('stock.warehouse', string='Warehouse')

    date_start = fields.Datetime(
        string="Start Date",
        help="Starting datetime for the product promotion location validation\n"
             "The displayed value depends on the timezone set in your preferences."
    )
    date_end = fields.Datetime(
        string="End Date",
        help="Ending datetime for the product promotion location validation\n"
             "The displayed value depends on the timezone set in your preferences."
    )

    price_tax_included = fields.Float(
        string="Price (Tax Included)",
        digits=(16, 4),
        help="Manual price including taxes with 4 decimal precision."
    )

    pricelist_ids = fields.Many2many(
        'product.pricelist',
        string="Pricelists",
        help="If set, the promotion will only apply when the POS uses one of these pricelists. If left empty, it applies to all pricelists."
    )

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    
    @api.constrains('date_start', 'date_end')
    def _check_date_range(self):
        for rec in self:
            if rec.date_start and rec.date_end:
                if rec.date_end <= rec.date_start:
                    raise ValidationError(
                        _("End Date must be greater than Start Date.")
                    )
