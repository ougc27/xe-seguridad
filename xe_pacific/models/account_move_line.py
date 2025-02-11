from odoo import fields, models, api
from odoo.tools import frozendict


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'
    
    x_order_id = fields.Many2one('sale.order',
        related='sale_line_ids.order_id',
        store=True,
        readonly=True
    )

    @api.depends('account_id', 'partner_id', 'product_id', 'x_order_id')
    def _compute_analytic_distribution(self):
        cache = {}
        for line in self:
            if line.display_type == 'product' or not line.move_id.is_invoice(include_receipts=True):
                partner_id = line.partner_id
                if partner_id.parent_id:
                    partner_id = partner_id.parent_id
                team_id = line.move_id.team_id.id
                if line.move_id.purchase_order_count:
                    team_id = None
                warehouse_id = line.x_order_id.warehouse_id.id
                if not line.x_order_id:
                    order = self.env['sale.order'].search([
                        ('name', '=', line.move_id.invoice_origin),
                        ('company_id', '=', line.move_id.company_id.id)
                    ])
                    if order:
                        warehouse_id = order.warehouse_id.id
                    else:
                        warehouse_id = line.move_id.warehouse_id.id
                arguments = frozendict({
                    "product_id": line.product_id.id,
                    "product_categ_id": line.product_id.categ_id.id,
                    "partner_id": partner_id.id,
                    "partner_category_id": partner_id.category_id.ids,
                    "account_prefix": line.account_id.code,
                    "company_id": line.company_id.id,
                    "warehouse_id": warehouse_id,
                    "team_id": team_id
                })
                if arguments not in cache:
                    cache[arguments] = self.env['account.analytic.distribution.model']._get_distribution(arguments)
                line.analytic_distribution = cache[arguments] or line.analytic_distribution
