from odoo import models, api


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    @api.depends('order_id.partner_id', 'product_id', 'order_id.warehouse_id')
    def _compute_analytic_distribution(self):
        for line in self:
            if not line.display_type:
                partner_id = line.order_id.partner_id
                if partner_id.parent_id:
                    partner_id = partner_id.parent_id
                distribution = line.env['account.analytic.distribution.model']._get_distribution({
                    "product_id": line.product_id.id,
                    "product_categ_id": line.product_id.categ_id.id,
                    "partner_id": line.order_id.partner_id.id,
                    "partner_category_id": line.order_id.partner_id.category_id.ids,
                    "company_id": line.company_id.id,
                    "warehouse_id": line.order_id.warehouse_id.id,
                    "team_id": line.order_id.team_id.id,
                })
                line.analytic_distribution = distribution or line.analytic_distribution
