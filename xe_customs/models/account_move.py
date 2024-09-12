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
        compute='_get_source_orders',
        copy=False
    )
    reconciled_amount = fields.Monetary(
        string="Reconciled Amount",
        compute='_get_source_orders',
        copy=False
    )
    reconcile_balance = fields.Monetary(
        string="Reconcile Balance",
        default=lambda self: self.amount_total,
        copy=False
    )
    
    def _get_source_orders(self):
        for move in self:
            move.source_orders = move.line_ids.sale_line_ids.order_id
            move.source_orders.down_payment_context = 0
            for order_line in move.line_ids.sale_line_ids:
                if move.id in order_line.invoice_lines.move_id.ids:
                    order_line.order_id.down_payment_context += order_line.price_unit * (1 + (order_line.tax_id[0].amount / 100))
            move.reconciled_amount = sum(move.source_orders.mapped('down_payment_context'))
            move.reconcile_balance = move.amount_total - move.reconciled_amount 

    def _get_down_payment_context(self):
            # Relate the down payment's l10n_mx_edi_cfdi_uuid to the invoice's l10n_mx_edi_cfdi_origin with code 07| and uuid comma separated
        for order in self:
            value = 0
            order.down_payment_context = value
