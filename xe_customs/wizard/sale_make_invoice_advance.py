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
        
        downpayments = self.env['sale.down.payment'].search([
            ('order_id', '=', orders.id),
        ])
        lines = [(0, 0, {
            'invoice_id': downpayment.invoice_id.id,
            'amount': downpayment.order_line_id.price_unit * (1 + (downpayment.order_line_id.tax_id[0].amount / 100)),
            'downpayment_id': downpayment.id,
        }) for downpayment in downpayments]

        lines += [(0, 0, {
            'invoice_id': invoice.id,
            'amount': 0,
        }) for invoice in self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('partner_id', 'in', orders.mapped('partner_id').ids),
            ('has_down_payment', '=', True),
            ('state', '!=', 'cancel'),
            ('id', 'not in', downpayments.mapped('invoice_id').ids),
        ])]

        return lines

    def _create_invoices(self, sale_orders):
        invoices = super(SaleMakeInvoiceAdvance, self)._create_invoices(sale_orders)
        if self.advance_payment_method == 'percentage':
            raise UserError('The percentage method is not supported for down payments.')

        if self.advance_payment_method != 'delivered':
            for invoice in invoices:
                for order_line in invoice.line_ids.sale_line_ids:
                    self.env['sale.down.payment'].create({
                        'order_line_id': order_line.id,
                        'invoice_id': invoice.id,
                        'amount': order_line.price_unit * (1 + (order_line.tax_id[0].amount / 100)),
                    })
        else:
            order_id = self.env['sale.order'].browse(self._context.get('active_ids', []))[0]
            product_id = order_id.company_id.sale_down_payment_product_id
            tax_id = product_id.taxes_id[0]

            # Check down payments total
            if self.reconciled_amount > order_id.amount_total:
                raise UserError('The total amount of down payments exceeds the total amount of the order.')
            
            # Update down payments
            downpayments = self.down_payment_ids.filtered(lambda x: x.amount > 0 and x.downpayment_id)
            for downpayment in downpayments:
                downpayment.downpayment_id.order_line_id.price_unit = downpayment.amount / (1 + (tax_id.amount / 100))

            # Register down payments
            downpayments = self.down_payment_ids.filtered(lambda x: x.amount > 0 and not x.downpayment_id)
            for downpayment in downpayments:
                dp = self.env['sale.down.payment'].create({
                    'invoice_id': downpayment.invoice_id.id,
                })
                dp._prepare_lines(self.env['sale.order'].browse(self._context.get('active_ids', []))[0], downpayment.amount)
                

            for invoice in invoices:
                origins = []
                for inv in invoice.line_ids.sale_line_ids.order_id.down_payment_ids:
                    if inv.l10n_mx_edi_cfdi_uuid:
                        origins.append(inv.l10n_mx_edi_cfdi_uuid)
                if len(origins) > 0:
                    invoice.l10n_mx_edi_cfdi_origin = '07|' + ','.join(origins)

        invoices.locked = True
        if self.advance_payment_method == 'delivered':
            invoices.auto_credit_note = True

        return invoices

    @api.depends('sale_order_ids', 'reconciled_amount')
    def _compute_invoice_amounts(self):
        super(SaleMakeInvoiceAdvance, self)._compute_invoice_amounts()
        for wizard in self:
            order_id = self.env['sale.order'].browse(self._context.get('active_ids', []))[0]
            wizard.amount_invoiced += order_id.reconciled_amount
            wizard.amount_to_invoice -= order_id.reconciled_amount



class SaleDownPaymentWizard(models.TransientModel):
    _name = "sale.down.payment.wizard"
    _description = "Sale Down Payment"

    advance_id = fields.Many2one('sale.advance.payment.inv')
    downpayment_id = fields.Many2one(
        comodel_name = 'sale.down.payment',
    )
    invoice_id = fields.Many2one(
        comodel_name = 'account.move',
        string = "Invoice",
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
    )

    @api.onchange('amount')
    def _onchange_amount(self):
        for payment in self:
            if payment.amount > payment.balance:
                payment.amount = payment.balance
                return {
                    'warning': {
                        'title': "Warning",
                        'message': "The amount is greater than the balance. It was reset to the balance.",
                    }
                }
