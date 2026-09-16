from odoo import api, fields, models


class MeliSkuMapping(models.Model):
    _name = 'meli.sku.mapping'
    _description = 'Mercado Libre SKU Mapping'

    product_id = fields.Many2one(
        'product.product', string='Product', required=True, ondelete='cascade',
    )
    meli_sku = fields.Char(string='Mercado Libre SKU', required=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [(
        'meli_sku_uniq', 'unique(meli_sku)',
        'This Mercado Libre SKU is already mapped to another product.',
    )]

    @api.model
    def _find_product_by_meli_sku(self, meli_sku):
        sku = (meli_sku or '').strip()
        if not sku:
            return self.env['product.product']
        mapping = self.search([('meli_sku', '=', sku)], limit=1)
        return mapping.product_id

    @api.model
    def _resolve_product_by_meli_sku(self, meli_sku):
        """Resolve a product for a Mercado Libre SKU. An exact match on
        the product's own default_code always wins — the mapping table is
        only consulted as a fallback, for the cases where Mercado Libre's
        SKU doesn't match Odoo's code directly (e.g. a listing SKU with a
        stray hyphen). Most products never need a row in this table at
        all.
        """
        sku = (meli_sku or '').strip()
        if not sku:
            return self.env['product.product']
        product = self.env['product.product'].search(
            [('default_code', '=', sku)], limit=1,
        )
        if product:
            return product
        return self._find_product_by_meli_sku(sku)
