from odoo import models, api


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    @api.depends('product_id', 'order_id.partner_id', 'order_id.picking_type_id')
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
                
                additional_distribution = line.env['account.analytic.distribution.model']._get_distribution({
                    "company_id": line.company_id.id,
                    "warehouse_id": line.order_id.picking_type_id.warehouse_id.id
                })

                #if initial_distribution == additional_distribution:
                    #line.analytic_distribution = initial_distribution or line.analytic_distribution
    
                combined_distribution = {}
                for dist in [initial_distribution, additional_distribution]:
                    if dist:
                        for key, value in dist.items():
                            combined_distribution[key] = combined_distribution.get(key, 0) + value
                
                line.analytic_distribution = combined_distribution or line.analytic_distribution
