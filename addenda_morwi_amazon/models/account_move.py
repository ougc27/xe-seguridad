from odoo import models, fields


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    def get_amazon_po_fixed(self):
        """
            return : Devuelve un valor estático para la addenda
        """
        return 'AmazonPO'

    def _l10n_mx_edi_add_invoice_cfdi_values(self, cfdi_values, percentage_paid=None, global_invoice=False):
        self.ensure_one()
        res = super(AccountMove, self)._l10n_mx_edi_add_invoice_cfdi_values(cfdi_values, percentage_paid, global_invoice)
        addenda_id = self.env.ref('addenda_morwi_amazon.l10n_mx_edi_addenda_amazon')
        is_amazon_addenda = False
        for concepto_list in cfdi_values['conceptos_list']:
            partner = concepto_list['line'].get('partner', None)
            product = concepto_list['line'].get('product', None)
            if partner and product and partner.l10n_mx_edi_addenda:
                if partner.l10n_mx_edi_addenda.id == addenda_id.id:
                    is_amazon_addenda = True
                    product_name = self.env['product.clientinfo'].search([
                        ('name', '=', partner.id),
                        ('product_tmpl_id', '=', product.product_tmpl_id.id)
                    ], limit=1).product_name
                    if product_name:
                        concepto_list['no_identificacion'] = product_name
        if is_amazon_addenda:
            cfdi_values['condiciones_de_pago'] = f"PO:{self.payment_reference}"
        return res