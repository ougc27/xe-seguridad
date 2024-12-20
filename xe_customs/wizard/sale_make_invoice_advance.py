# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields, api, _
from odoo.fields import Command

from odoo.exceptions import UserError

class SaleMakeInvoiceAdvance(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'

    down_payment_ids = fields.One2many('sale.down.payment.wizard', 'advance_id', "Down Payments", default=lambda self: self._default_down_payment_ids())
    down_payment_ids_count = fields.Integer(
        string="Down Payments",
        compute="_compute_down_payment_ids_count",
    )
    reconciled_amount = fields.Monetary(
        string="Reconciled Amount",
        compute="_compute_reconciled_amount",
    )

    def _compute_reconciled_amount(self):
        self.reconciled_amount = sum(self.down_payment_ids.mapped('amount'))

    def _compute_down_payment_ids_count(self):
        self.down_payment_ids_count = len(self.down_payment_ids)

    def _default_down_payment_ids(self):
        orders = self.env['sale.order'].browse(self._context.get('active_ids', []))
        if len(orders) > 1:
            return []
        
        downpayments = self.env['sale.down.payment'].search([
            ('order_id', '=', orders.id),
        ])
        lines = [(0, 0, {
            'invoice_id': downpayment.invoice_id.id,
            'amount': downpayment.order_line_id.price_unit * (1 + sum(tax.amount for tax in downpayment.order_line_id.tax_id) / 100),
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
        order_id = self.env['sale.order'].sudo().browse(self._context.get('active_ids', []))
        if len(order_id) > 1:
            return super(SaleMakeInvoiceAdvance, self)._create_invoices(sale_orders)

        self.ensure_one()
        product_id = order_id.company_id.sudo().sale_down_payment_product_id
        tax_id = product_id.with_context(company_id=order_id.company_id.id).taxes_id.sudo().filtered(lambda x: x.company_id == order_id.company_id)
        
        # Create deposit product if necessary
        if not product_id:
            self.company_id.sudo().sale_down_payment_product_id = self.env['product.product'].create(
                self._prepare_down_payment_product_values()
            )
            product_id = order_id.company_id.sudo().sale_down_payment_product_id
            tax_id = product_id.with_context(company_id=order_id.company_id.id).taxes_id.filtered(lambda x: x.company_id == order_id.company_id)
        
        total_tax = sum(tax_id.mapped('amount'))

        if self.advance_payment_method == 'percentage':
            raise UserError('The percentage method is not supported for down payments.')
        elif self.advance_payment_method == 'delivered':
            invoices = sale_orders._create_invoices(final=self.deduct_down_payments, grouped=not self.consolidated_billing)
            
            # Check down payments total
            if self.reconciled_amount > order_id.amount_total:
                raise UserError('The total amount of down payments exceeds the total amount of the order.')
            
            # Update down payments
            downpayments = self.down_payment_ids.filtered(lambda x: x.amount > 0 and x.downpayment_id)
            for downpayment in downpayments:
                downpayment.downpayment_id.order_line_id.price_unit = downpayment.amount / (1 + (total_tax / 100))

            # Register down payments
            downpayments = self.down_payment_ids.filtered(lambda x: x.amount > 0 and not x.downpayment_id)
            for downpayment in downpayments:
                dp = self.env['sale.down.payment'].create({
                    'invoice_id': downpayment.invoice_id.id,
                })
                dp._prepare_lines(order_id, downpayment.amount)
                

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
        else:
            self = self.with_company(self.company_id)
            invoice = self.env['account.move'].sudo().create({
                **order_id._prepare_invoice(),
                'invoice_line_ids': [(0, 0, {
                    'product_id': product_id.id,
                    'quantity': 1.0,
                    'price_unit': self.fixed_amount / (1 + (total_tax / 100)),
                    'tax_ids': [(6, 0, tax_id.ids)],
                    'is_downpayment': True,
                    'name': _('Down Payment'),
                    'sequence': 10,
                })],
            })

            # Ensure the invoice total is exactly the expected fixed amount.
            if self.advance_payment_method == 'fixed':
                delta_amount = (invoice.amount_total - self.fixed_amount) * (1 if invoice.is_inbound() else -1)
                if not order_id.currency_id.is_zero(delta_amount):
                    receivable_line = invoice.line_ids\
                        .filtered(lambda aml: aml.account_id.account_type == 'asset_receivable')[:1]
                    product_lines = invoice.line_ids\
                        .filtered(lambda aml: aml.display_type == 'product')
                    tax_lines = invoice.line_ids\
                        .filtered(lambda aml: aml.tax_line_id.amount_type not in (False, 'fixed'))

                    if product_lines and tax_lines and receivable_line:
                        line_commands = [Command.update(receivable_line.id, {
                            'amount_currency': receivable_line.amount_currency + delta_amount,
                        })]
                        delta_sign = 1 if delta_amount > 0 else -1
                        for lines, attr, sign in (
                            (product_lines, 'price_total', -1),
                            (tax_lines, 'amount_currency', 1),
                        ):
                            remaining = delta_amount
                            lines_len = len(lines)
                            for line in lines:
                                if order_id.currency_id.compare_amounts(remaining, 0) != delta_sign:
                                    break
                                amt = delta_sign * max(
                                    order_id.currency_id.rounding,
                                    abs(order_id.currency_id.round(remaining / lines_len)),
                                )
                                remaining -= amt
                                line_commands.append(Command.update(line.id, {attr: line[attr] + amt * sign}))
                        invoice.line_ids = line_commands

            # Unsudo the invoice after creation if not already sudoed
            invoice = invoice.sudo(self.env.su)

            poster = self.env.user._is_internal() and self.env.user.id or SUPERUSER_ID
            invoice.with_user(poster).message_post_with_source(
                'mail.message_origin_link',
                render_values={'self': invoice, 'origin': order_id},
                subtype_xmlid='mail.mt_note',
            )

            title = _("Down payment invoice")
            order_id.with_user(poster).message_post(
                body=_("%s has been created", invoice._get_html_link(title=title)),
            )

    @api.depends('sale_order_ids', 'reconciled_amount')
    def _compute_invoice_amounts(self):
        super(SaleMakeInvoiceAdvance, self)._compute_invoice_amounts()
        order_id = self.env['sale.order'].browse(self._context.get('active_ids', []))
        if len(order_id) > 1:
            return

        for wizard in self:
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
        compute = "_compute_balance",
    )
    amount = fields.Monetary(
        string = "Amount",
    )

    def _compute_balance(self):
        for payment in self:
            payment.balance = payment.invoice_id.reconcile_balance + payment.amount

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
