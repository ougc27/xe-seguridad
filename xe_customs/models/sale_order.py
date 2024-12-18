# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models, _

from odoo.exceptions import UserError, AccessError

class SaleOrder(models.Model):
    _inherit = "sale.order"

    down_payment_context = fields.Monetary(
        string="Reconciled Amount",
        copy=False
    )
    down_payment_ids = fields.One2many(
        comodel_name='sale.down.payment',
        inverse_name='order_id',
        string="Invoices",
        copy=False,
        readonly=False
    )
    reconciled_amount = fields.Monetary(
        string="Reconciled Amount",
        compute="_compute_reconciled_amount",
    )

    def _compute_reconciled_amount(self):
        for order in self:
            reconciled_lines = order.order_line.filtered(lambda x: x.is_downpayment)
            total_tax = sum(reconciled_lines.tax_id.mapped('amount'))
            order.reconciled_amount = sum(reconciled_lines.mapped('price_unit')) * (1 + (total_tax / 100))

    def _add_client_to_product(self):
        # Add the partner in the client list of the product if the client is not registered for
        # this product. We limit to 10 the number of clients for a product to avoid the mess that
        # could be caused for some generic products ("Miscellaneous").
        for line in self.order_line:
            # Do not add a contact as a client
            partner = line.order_id.partner_id if not line.order_id.partner_id.parent_id else line.order_id.partner_id.parent_id
            already_client = (partner | line.order_id.partner_id) & line.product_id.client_ids.mapped('name')
            if line.product_id and not already_client and len(line.product_id.client_ids) <= 10:
                # Convert the price in the right currency.
                currency = partner.property_purchase_currency_id or line.env.company.currency_id
                price = line.currency_id._convert(line.price_unit, currency, line.company_id, line.order_id.date_order or fields.Date.today(), round=False)
                # Compute the price for the template's UoM, because the client's UoM is related to that UoM.
                if line.product_id.product_tmpl_id.uom_po_id != line.product_uom:
                    default_uom = line.product_id.product_tmpl_id.uom_po_id
                    price = line.product_uom._compute_price(price, default_uom)

                clientinfo = line._prepare_client_info(partner, line, price, currency)
                # In case the order partner is a contact address, a new clientinfo is created on
                # the parent company. In this case, we keep the product name and code.
                client = line.product_id._select_client(
                    partner_id=line.order_id.partner_id,
                    quantity=line.product_uom_qty,
                    date=line.order_id.date_order and line.order_id.date_order.date(),
                    uom_id=line.product_uom)
                if client:
                    clientinfo['product_name'] = client.product_name
                    clientinfo['product_code'] = client.product_code
                vals = {
                    'client_ids': [(0, 0, clientinfo)],
                }
                try:
                    line.product_id.write(vals)
                except AccessError:  # no write access rights -> just ignore
                    break

    # @api.onchange('order_line')
    # def _compute_amount_to_invoice(self):
    #     super(SaleOrder, self)._compute_amount_to_invoice()
    #     raise Exception(self.invoiced_amount)

    def action_cancel(self):
        for order in self:
            if len(order.down_payment_ids) > 0:
                raise UserError(_('You cannot cancel an order with down payments.'))
            else:
                return super(SaleOrder, order).action_cancel()

    def action_confirm(self):
        for order in self:
            res = super(SaleOrder, order).action_confirm()
            order._add_client_to_product()
            return res


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    margin = fields.Monetary("Margin", compute='_compute_margin', store=True, groups="account.group_account_manager")

    client_barcode = fields.Char(
        string='Client Product Barcode',
        compute='_compute_client_barcode',
        store=True)

    @api.depends('order_id.partner_id', 'product_id')
    def _compute_client_barcode(self):
        for line in self:
            if not line.product_id or not line.order_id.partner_id:
                line.client_barcode = False
                return
            client = line.product_id._select_client(
                partner_id=line.order_id.partner_id,
                quantity=line.product_uom_qty,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom)
            if client:
                line.client_barcode = client.product_barcode
            else:
                line.client_barcode = False

    def _prepare_client_info(self, partner, line, price, currency):
        # Prepare clientinfo data when adding a product
        return {
            'name': partner.id,
            'sequence': max(line.product_id.client_ids.mapped('sequence')) + 1 if line.product_id.client_ids else 1,
            'min_qty': 0.0,
            'price': price,
            'currency_id': currency.id,
            'delay': 0,
        }

    @api.onchange('product_id', 'product_uom_qty')
    def onchange_product_id(self):
        for line in self:
            if not line.product_id:
                return

            client = line.product_id._select_client(
                partner_id=line.order_id.partner_id,
                quantity=line.product_uom_qty,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom)
            if client:
                line.price_unit = client.price
                name = ""
                if client.product_code:
                    name += "[%s] " % client.product_code
                elif line.product_id.default_code:
                    name += "[%s] " % line.product_id.default_code
                if client.product_name:
                    name += client.product_name
                else:
                    name += line.product_id.name
                line.name = name
            else:
                line.price_unit = line.product_id.lst_price
                line.name = line.product_id.name


class SaleDownPayment(models.Model):
    _name = "sale.down.payment"
    _description = "Sale Down Payment"

    invoice_id = fields.Many2one(
        comodel_name = 'account.move',
        string = "Invoice",
        copy = False
    )
    l10n_mx_edi_cfdi_uuid = fields.Char(
        string = "Fiscal Folio",
        related = 'invoice_id.l10n_mx_edi_cfdi_uuid',
    )
    order_line_id = fields.Many2one(
        comodel_name = 'sale.order.line',
        string = "Order Line",
        copy = False
    )
    order_id = fields.Many2one(
        comodel_name = 'sale.order',
        string = "Order",
        related = 'order_line_id.order_id',
        copy = False
    )
    currency_id = fields.Many2one(
        comodel_name = 'res.currency',
        string = "Currency",
        related = 'invoice_id.currency_id',
    )
    amount = fields.Monetary(
        related = 'invoice_id.amount_total',
        string = "Amount",
        copy = False
    )
    reconciled_amount = fields.Float(
        string = "Reconciled Amount",
        compute = "_compute_reconciled_amount",
    )

    def _compute_reconciled_amount(self):
        for payment in self:
            total_tax = sum(payment.order_line_id.tax_id.mapped('amount'))
            payment.reconciled_amount = payment.order_line_id.price_unit * (1 + (total_tax / 100))

    # @api.onchange('invoice_id')
    # def _onchange_invoice_id(self):
    #     for payment in self:
    #         if payment.invoice_id:
    #             order_id = payment.order_id
    #             payment.amount = payment.invoice_id.amount_total
    #             amount = payment.invoice_id.reconcile_balance
    #             self._prepare_lines(order_id, amount)

    def _prepare_lines(self, order_id, amount):
        for payment in self:
            product_id = order_id.company_id.sudo().sale_down_payment_product_id
            tax_id = product_id.with_context(company_id=order_id.company_id.id).taxes_id.sudo()
            total_tax = sum(tax_id.mapped('amount'))
            
            # Create down payment section if necessary
            section = self.env['sale.order.line'].with_context(sale_no_log_for_new_lines=True)
            if not any(line.display_type and line.is_downpayment for line in order_id.order_line):
                section.create(
                    self._prepare_down_payment_section_values(order_id)
                )

            # Add new down payment line on Invoice
            invoice_down_payment = self.env['account.move.line'].create({
                'move_id': payment.invoice_id.id,
                'product_id': product_id.id,
                'quantity': 0,
                'price_unit': amount / (1 + (total_tax / 100)),
                'tax_ids': [(6, 0, tax_id.ids)],
                'is_downpayment': True,
                'name': _('Down Payment'),
                'sequence': payment.invoice_id.invoice_line_ids and payment.invoice_id.invoice_line_ids[-1].sequence + 1 or 10,
            })

            # Add new down payment line on Sale
            sale_down_payment = order_id.order_line.create({
                'product_id': product_id.id,
                'order_id': order_id._origin.id,
                'product_uom_qty': 0,
                'discount': 0.0,
                'price_unit': amount / (1 + (total_tax / 100)),
                'tax_id': [(6, 0, tax_id.ids)],
                'is_downpayment': True,
                'invoice_lines': [(6, 0, invoice_down_payment.ids)],
                'sequence': order_id.order_line and order_id.order_line[-1].sequence + 1 or 10,
            })
            payment.order_line_id = sale_down_payment
            invoice_down_payment.sale_line_ids += sale_down_payment
            payment.invoice_id._get_source_orders()

    def unlink(self):
        for payment in self:
            payment.order_line_id.invoice_lines.write({
                'sale_line_ids': False,
                'name': _('Unlinked Down Payment'),
            })
            payment.order_line_id.write({
                'invoice_lines': False,
                'price_unit': 0,
                'name': _('Deleted Down Payment'),
            })
            payment.invoice_id._get_source_orders()
        return super(SaleDownPayment, self).unlink()

    def _prepare_down_payment_section_values(self, order):
        context = {'lang': order.partner_id.lang}

        so_values = {
            'name': _('Down Payments'),
            'product_uom_qty': 0.0,
            'order_id': order._origin.id,
            'display_type': 'line_section',
            'is_downpayment': True,
            'sequence': order.order_line and order.order_line[-1].sequence + 1 or 10,
        }

        del context
        return so_values
  