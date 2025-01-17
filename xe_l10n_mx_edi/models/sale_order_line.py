from odoo import api, models


class SaleOrderLine(models.Model):
    
    _inherit = 'sale.order.line'

    @api.depends('order_partner_id')
    def _compute_tax_id(self):
        super()._compute_tax_id()
        for line in self:
            partner = line.order_partner_id
            if partner.parent_id:
                partner = partner.parent_id
            if partner.is_border_zone_iva:
                tax = self.env.ref('account.3_tax17', raise_if_not_found=False)
                if line.company_id.id == 1:
                    tax = self.env.ref('account.1_tax17', raise_if_not_found=False)
                if line.company_id.id == 4:
                    tax = self.env.ref('account.4_tax17', raise_if_not_found=False)
                if tax:
                    line.tax_id = tax
