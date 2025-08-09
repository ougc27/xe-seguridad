from odoo import models, api, _
from odoo.fields import Command
from odoo.exceptions import UserError


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def restrict_unit_price_zero(self):
        for rec in self:
            if rec.product_uom_qty > 0 and rec.price_unit < 0.01:
                product = rec.product_template_id
                if product:
                    if product.default_code not in ['ANT', 'DESC']:
                        raise UserError(_("Items with a unit price of $0 are not allowed, please modify it to $0.01 (one cent)"))   

    @api.model
    def create(self, vals):
        res = super().create(vals)
        res.restrict_unit_price_zero()
        return res

    def write(self, vals):
        res = super().write(vals)
        self.restrict_unit_price_zero()
        return res

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
                    "partner_id": partner_id.id,
                    "partner_category_id": partner_id.category_id.ids,
                    "company_id": line.company_id.id,
                    "warehouse_id": line.order_id.warehouse_id.id,
                    "team_id": line.order_id.team_id.id,
                })
                line.analytic_distribution = distribution or line.analytic_distribution

    @api.depends('move_ids.state', 'move_ids.picking_id.state', 'move_ids.quantity', 'move_ids.product_uom')
    def _compute_qty_delivered(self):
        super()._compute_qty_delivered()
        for sale_line in self:
            order = sale_line.order_id

            if sale_line.product_id.type != 'service' and sale_line.qty_delivered > 0:
                shipping_line = order.order_line.filtered(
                    lambda l: l.product_id.default_code == 'SHIPPING-VA' and l.product_id.type == 'service'
                )
                for line in shipping_line:
                    line.qty_delivered = line.product_uom_qty
