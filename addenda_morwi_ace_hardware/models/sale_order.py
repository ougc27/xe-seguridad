from odoo import models


class SaleOrder(models.Model):
    _inherit = 'sale.order'
    
    def _get_vendor_number_addenda(self):
        vendor_number_addenda = False
        if self.partner_id and self.partner_id.l10n_mx_edi_addenda:
            product_tmpl_ids = self.order_line.mapped("product_id.product_tmpl_id.id")
            domain = [('product_tmpl_id','in', product_tmpl_ids),('product_code','!=', False),('partner_id.ref','!=', False)]
            prod_suppl_id = self.env['product.supplierinfo'].search(domain, limit=1)
            if prod_suppl_id:
                vendor_number_addenda = prod_suppl_id.partner_id.ref
        return vendor_number_addenda
    
    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        if res:
            res['purchase_order_reference'] = self.reference
            res['vendor_number_addenda'] = self._get_vendor_number_addenda()
        return res