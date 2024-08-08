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
        compute='_get_source_orders',
        copy=False
    )
    
    def _get_source_orders(self):
        for invoice in self:
            invoice.source_orders = invoice.line_ids.sale_line_ids.order_id
            invoice.source_orders.down_payment_context = 0
            for order_line in invoice.line_ids.sale_line_ids:
                if invoice.id in order_line.invoice_lines.move_id.ids:
                    order_line.order_id.down_payment_context += order_line.price_unit * (1 + (order_line.tax_id[0].amount / 100))
            invoice.reconciled_amount = sum(invoice.source_orders.mapped('down_payment_context'))
            invoice.reconcile_balance = invoice.amount_total - invoice.reconciled_amount 

    def _get_down_payment_context(self):
            # Relate the down payment's l10n_mx_edi_cfdi_uuid to the invoice's l10n_mx_edi_cfdi_origin with code 07| and uuid comma separated
        for order in self:
            value = 0
            order.down_payment_context = value

    def action_post(self):
        # Cambiar prorateo por una sola linea de anticipo
        for invoice in self:
            res = super(AccountMove, self).action_post()
            lines = []
            down_payment = sum(invoice.line_ids.filtered(lambda x: x.is_downpayment).mapped('price_subtotal'))
            tax_ids = invoice.line_ids.tax_ids.ids
            if down_payment < 0:
                origin = False
                if invoice.l10n_mx_edi_cfdi_uuid:
                    origin = '07|' + invoice.l10n_mx_edi_cfdi_uuid
                credit_note = self.env['account.move'].create({
                    'move_type': 'out_refund',
                    'partner_id': invoice.partner_id.id,
                    'journal_id': invoice.journal_id.id,
                    'date': fields.Date.today(),
                    'ref': _('Down Payments of: %s', invoice.name),
                    'l10n_mx_edi_cfdi_uuid': origin,
                    'line_ids': [(0, 0, {
                        'product_id': invoice.company_id.sale_down_payment_product_id.id,
                        'name': _("Down Payment"),
                        'quantity': 1,
                        'price_unit': down_payment * - 1,
                        'tax_ids': [(6, 0, tax_ids)]
                    })]
                })
                credit_note.action_post()
        return res
