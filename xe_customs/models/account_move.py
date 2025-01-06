# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models, _

from odoo.exceptions import UserError

class AccountMove(models.Model):
    _inherit = "account.move"

    source_orders = fields.Many2many(
        comodel_name='sale.order',
        string="Invoices",
        compute='_get_source_orders'
    )
    reconciled_amount = fields.Monetary(
        string="Reconciled Amount",
        compute='_get_source_orders'
    )
    reconcile_balance = fields.Monetary(
        string="Reconcile Balance",
        copy=False
    )
    has_down_payment = fields.Boolean(
        string="Has Down Payment",
        copy=False,
    )
    hidden_for_down_payment = fields.Boolean(
        string="Hidden for Down Payments",
        copy=False,
        default=False
    )
    locked = fields.Boolean(default=False)
    auto_credit_note = fields.Boolean(default=False)
    remarks = fields.Char(copy=False)
    
    @api.depends('invoice_line_ids')
    def _get_source_orders(self):
        for move in self:
            source_orders = move.invoice_line_ids.sale_line_ids.order_id
            source_orders.down_payment_context = 0
            for order_line in move.invoice_line_ids.sale_line_ids:
                if move.id in order_line.invoice_lines.move_id.ids:
                    tax_id = order_line.tax_id.sudo()
                    total_tax = sum(tax_id.mapped('amount'))
                    order_line.order_id.down_payment_context += order_line.price_unit * (1 + (total_tax / 100))
            move.reconciled_amount = sum(source_orders.mapped('down_payment_context'))
            move.reconcile_balance = sum(move.invoice_line_ids.filtered(lambda x: x.product_id.id == x.company_id.sale_down_payment_product_id.id).mapped('price_total')) - move.reconciled_amount
            move.has_down_payment = move.reconcile_balance > 0
            move.source_orders = source_orders
