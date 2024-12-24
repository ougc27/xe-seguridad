from odoo import models, api
from odoo.tools import frozendict


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    @api.depends('order_id.partner_id', 'product_id', 'order_id.warehouse_id')
    def _compute_analytic_distribution(self):
        for line in self:
            if not line.display_type:
                initial_distribution = line.env['account.analytic.distribution.model']._get_distribution({
                    "product_id": line.product_id.id,
                    "product_categ_id": line.product_id.categ_id.id,
                    "partner_id": line.order_id.partner_id.id,
                    "partner_category_id": line.order_id.partner_id.category_id.ids,
                    "company_id": line.company_id.id,
                })
                
                warehouse_distribution = line.env['account.analytic.distribution.model']._get_distribution({
                    "company_id": line.company_id.id,
                    "warehouse_id": line.order_id.warehouse_id.id,
                })

                team_distribution = line.env['account.analytic.distribution.model']._get_distribution({
                    "company_id": line.company_id.id,
                    "team_id": line.order_id.team_id.id
                })
    
                combined_distribution = {}
                for dist in [initial_distribution, warehouse_distribution, team_distribution]:
                    if dist:
                        for key, value in dist.items():
                            combined_distribution[key] = combined_distribution.get(key, 0) + value
                
                line.analytic_distribution = combined_distribution or line.analytic_distribution
