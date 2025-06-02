# coding: utf-8

from odoo import api, fields, models
from odoo.tools.sql import column_exists, create_column


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _auto_init(self):
        if not column_exists(self.env.cr, "sale_order", "amount_to_billing"):
            create_column(self.env.cr, "sale_order", "amount_to_billing", "numeric")
        return super()._auto_init()

    @api.depends('order_line.qty_to_invoice', 'order_line.price_unit', 'invoice_status')
    def _compute_amount_to_billing(self):
        for rec in self:
            amount_to_inv = 0.0
            if rec.state == 'sale' and rec.invoice_status == 'to invoice':
                target_lines = rec.order_line.filtered(lambda l: l.qty_to_invoice >= 1 and l.product_id.id != 1789)
                amount_to_inv = sum(line.qty_to_invoice * line.price_unit for line in target_lines)
            rec['amount_to_billing'] = amount_to_inv
 
    amount_to_billing = fields.Monetary(
        string='Amount to billing',
        compute='_compute_amount_to_billing',
        store=True
    )
