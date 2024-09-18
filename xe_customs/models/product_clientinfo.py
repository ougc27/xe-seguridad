# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models, api

class ProductClientInfo(models.Model):
    _name = "product.clientinfo"
    _description = "Client Pricelist"
    _order = 'sequence, min_qty DESC, price, id'

    name = fields.Many2one(
        'res.partner', 'Client',
        ondelete='cascade', required=True,
        help="Client of this product", check_company=True)
    product_name = fields.Char(
        'Client Product Name',
        help="This client's product name will be used when printing a request for quotation. Keep empty to use the internal one.")
    product_code = fields.Char(
        'Client Product Code',
        help="This client's product code will be used when printing a request for quotation. Keep empty to use the internal one.")
    sequence = fields.Integer(
        'Sequence', default=1, help="Assigns the priority to the list of product client.")
    product_uom = fields.Many2one(
        'uom.uom', 'Unit of Measure',
        related='product_tmpl_id.uom_po_id',
        help="This comes from the product form.")
    min_qty = fields.Float(
        'Quantity', default=0.0, required=True, digits="Product Unit Of Measure",
        help="The quantity to purchase from this client to benefit from the price, expressed in the client Product Unit of Measure if not any, in the default unit of measure of the product otherwise.")
    price = fields.Float(
        'Price', default=0.0, digits='Product Price',
        required=True, help="The price to purchase a product")
    company_id = fields.Many2one(
        'res.company', 'Company',
        default=lambda self: self.env.company.id, index=1)
    currency_id = fields.Many2one(
        'res.currency', 'Currency',
        default=lambda self: self.env.company.currency_id.id,
        required=True)
    date_start = fields.Date('Start Date', help="Start date for this client price")
    date_end = fields.Date('End Date', help="End date for this client price")
    product_id = fields.Many2one(
        'product.product', 'Product Variant', check_company=True,
        help="If not set, the client price will apply to all variants of this product.")
    product_tmpl_id = fields.Many2one(
        'product.template', 'Product Template', check_company=True,
        index=True, ondelete='cascade')
    product_variant_count = fields.Integer('Variant Count', related='product_tmpl_id.product_variant_count')
    delay = fields.Integer(
        'Delivery Lead Time', default=1, required=True,
        help="Lead time in days between the confirmation of the purchase order and the receipt of the products in your warehouse. Used by the scheduler for automatic computation of the purchase order planning.")

    @api.model
    def get_import_templates(self):
        return [{
            'label': _('Import Template for client Pricelists'),
            'template': '/xe_customs/static/xls/product_clientinfo.xls'
        }]

    @api.constrains('product_id', 'product_tmpl_id')
    def _check_product_variant(self):
        for client in self:
            if client.product_id and client.product_tmpl_id and client.product_id.product_tmpl_id != client.product_tmpl_id:
                raise ValidationError(_('The product variant must be a variant of the product template.'))

    @api.onchange('product_tmpl_id')
    def _onchange_product_tmpl_id(self):
        """Clear product variant if it no longer matches the product template."""
        if self.product_id and self.product_id not in self.product_tmpl_id.product_variant_ids:
            self.product_id = False

    def _sanitize_vals(self, vals):
        """Sanitize vals to sync product variant & template on read/write."""
        # add product's product_tmpl_id if none present in vals
        if vals.get('product_id') and not vals.get('product_tmpl_id'):
            product = self.env['product.product'].browse(vals['product_id'])
            vals['product_tmpl_id'] = product.product_tmpl_id.id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._sanitize_vals(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._sanitize_vals(vals)
        return super().write(vals)
