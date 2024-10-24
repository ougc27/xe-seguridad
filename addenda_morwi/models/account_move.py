from odoo import fields, models, api


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    purchase_order_reference = fields.Char(copy=False)
    vendor_number_addenda = fields.Char(copy=False)
    
    @api.onchange('partner_id')
    def _onchange_partner_addenda(self):
        if self.partner_id and self.partner_id.l10n_mx_edi_addenda and self.move_type == 'out_invoice':
            self.vendor_number_addenda = self._get_vendor_number_addenda()
    
    def _get_vendor_number_addenda(self):
        vendor_number_addenda = False
        if self.partner_id.l10n_mx_edi_addenda and self.move_type == 'out_invoice':
            product_tmpl_ids = self.invoice_line_ids.mapped("product_id.product_tmpl_id.id")
            domain = [('product_tmpl_id','=', product_tmpl_ids),('product_code','!=', False),('partner_id.ref','!=', False)]
            prod_suppl_id = self.env['product.supplierinfo'].search(domain, limit=1)
            if prod_suppl_id:
                vendor_number_addenda = prod_suppl_id.partner_id.ref
        return vendor_number_addenda


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'
    
    discount_amount_addenda = fields.Float(copy=False, compute='_compute_discount_amount_addenda')
    sku_vendor_addenda = fields.Char(compute='_compute_addenda_sku_vendor')
    
    def _compute_discount_amount_addenda(self):
        for record in self:
            discount_amount_addenda = 0.0
            if record.move_type == 'out_invoice' and record.partner_id and record.partner_id.l10n_mx_edi_addenda:
                    discount_amount_addenda = record.currency_id.round(record.price_subtotal * (record.discount / 100.0))
            record.discount_amount_addenda = discount_amount_addenda

    def _get_addenda_sku_vendor(self):
        for record in self:
            sku_vendor = ""
            if record.product_id and record.partner_id and record.partner_id.l10n_mx_edi_addenda:
                domain = [('product_tmpl_id','=', record.product_id.product_tmpl_id.id),('product_code','!=', False)]
                prod_suppl_id = self.env['product.supplierinfo'].search(domain, limit=1)
                if prod_suppl_id:
                    sku_vendor = prod_suppl_id.product_code
            return sku_vendor