# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields, api, _

from odoo.exceptions import UserError

class SaleMakeInvoiceAdvance(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'

    down_payment_ids = fields.One2many('sale.down.payment.wizard', 'advance_id', "Down Payments", default=lambda self: self._default_down_payment_ids())
    reconciled_amount = fields.Monetary(
        string="Reconciled Amount",
        compute="_compute_reconciled_amount",
    )

    def _compute_reconciled_amount(self):
        self.reconciled_amount = sum(self.down_payment_ids.mapped('amount'))

    def _default_down_payment_ids(self):
        orders = self.env['sale.order'].browse(self._context.get('active_ids', []))
        if len(orders) > 1:
            raise UserError('You can only create one invoice at a time.')
        return [(0, 0, {
            'invoice_id': line.id,
            'amount': 0,
        }) for line in self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('partner_id', 'in', orders.mapped('partner_id').ids),
            ('has_down_payment', '=', True),
            ('state', '!=', 'cancel')
        ])]

    def _create_invoices(self, sale_orders):
        invoices = super(SaleMakeInvoiceAdvance, self)._create_invoices(sale_orders)
        if self.advance_payment_method == 'percentage':
            raise UserError('The percentage method is not supported for down payments.')

        # Check down payments total
        order_id = self.env['sale.order'].browse(self._context.get('active_ids', []))[0]
        if order_id.reconciled_amount + self.reconciled_amount > order_id.amount_total:
            raise UserError('The total amount of down payments exceeds the total amount of the order.')

        # Register down payments
        downpayments = self.down_payment_ids.filtered(lambda x: x.amount > 0)
        for downpayment in downpayments:
            dp = self.env['sale.down.payment'].create({
                'invoice_id': downpayment.invoice_id.id,
            })
            dp._prepare_lines(self.env['sale.order'].browse(self._context.get('active_ids', []))[0], downpayment.amount)

        if self.advance_payment_method != 'delivered':
            for invoice in invoices:
                for order_line in invoice.line_ids.sale_line_ids:
                    self.env['sale.down.payment'].create({
                        'order_line_id': order_line.id,
                        'invoice_id': invoice.id,
                        'amount': order_line.price_unit * (1 + (order_line.tax_id[0].amount / 100)),
                    })
        else:
            product_id = order_id.company_id.sale_down_payment_product_id
            tax_id = product_id.taxes_id[0]

            for invoice in invoices:
                origins = []
                for inv in invoice.line_ids.sale_line_ids.order_id.down_payment_ids:
                    if inv.l10n_mx_edi_cfdi_uuid:
                        origins.append(inv.l10n_mx_edi_cfdi_uuid)
                if len(origins) > 0:
                    invoice.l10n_mx_edi_cfdi_origin = '07|' + ','.join(origins)

                # for order_line in order_id.order_line.filtered(
                #     lambda x: x.is_downpayment and not x.display_type and x.price_unit > 0 # and x.qty_invoiced == 0
                # ):
                #     invoice_down_payment = self.env['account.move.line'].create({
                #         'move_id': invoice.id,
                #         'product_id': order_line.product_id.id,
                #         'quantity': -1,
                #         'price_unit': order_line.price_unit,
                #         'tax_ids': [(6, 0, tax_id.ids)],
                #         'is_downpayment': True,
                #         'name': _('Down Payment'),
                #     })

        invoices.locked = True
        if self.advance_payment_method == 'delivered':
            invoices.auto_credit_note = True

        return invoices



class SaleDownPaymentWizard(models.TransientModel):
    _name = "sale.down.payment.wizard"
    _description = "Sale Down Payment"

    advance_id = fields.Many2one('sale.advance.payment.inv')
    invoice_id = fields.Many2one(
        comodel_name = 'account.move',
        string = "Invoice",
        copy = False
    )
    l10n_mx_edi_cfdi_uuid = fields.Char(
        string = "Fiscal Folio",
        related = 'invoice_id.l10n_mx_edi_cfdi_uuid',
    )
    currency_id = fields.Many2one(
        comodel_name = 'res.currency',
        string = "Currency",
        related = 'invoice_id.currency_id',
    )
    balance = fields.Monetary(
        string = "Balance",
        related = 'invoice_id.reconcile_balance',
    )
    amount = fields.Monetary(
        string = "Amount",
        copy = False
    )

    def _onchange_amount(self):
        for payment in self:
            if payment.amount > payment.balance:
                payment.amount = payment.balance
                return {
                    'warning': {
                        'title': "Warning",
                        'message': "The amount is greater than the balance."
                    }
                }
