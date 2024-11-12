# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields, api

class SaleMakeInvoiceAdvance(models.TransientModel):
    _inherit = 'sale.advance.payment.inv'

    def _create_invoices(self, sale_orders):
        invoices = super(SaleMakeInvoiceAdvance, self)._create_invoices(sale_orders)
        if self.advance_payment_method != 'delivered':
            for invoice in invoices:
                for order_line in invoice.line_ids.sale_line_ids:
                    self.env['sale.down.payment'].create({
                        'order_line_id': order_line.id,
                        'invoice_id': invoice.id,
                        'amount': order_line.price_unit * (1 + (order_line.tax_id[0].amount / 100)),
                    })
        else:
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
