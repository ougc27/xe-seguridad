# -*- coding: utf-8 -*-
from markupsafe import Markup
from werkzeug.urls import url_encode

from odoo import _, api, fields, models, modules, tools, Command
from odoo.exceptions import UserError
from odoo.tools.misc import get_lang


class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'
    _description = "Account Move Send"

    move_ids = fields.Many2many(comodel_name='account.move')

    def action_send_and_print(self, force_synchronous=False, allow_fallback_pdf=False, **kwargs):
        res = super(AccountMoveSend, self).action_send_and_print(force_synchronous, allow_fallback_pdf, **kwargs)
        for move in self.move_ids:
            if len(move.line_ids.filtered(lambda x: x.is_downpayment)) == 0:
                down_payment = sum(move.source_orders.order_line.filtered(lambda x: x.is_downpayment).mapped('price_unit'))

                tax_ids = move.line_ids.tax_ids.ids
                if down_payment > 0:
                    origin = False
                    if move.l10n_mx_edi_cfdi_uuid:
                        origin = '07|' + move.l10n_mx_edi_cfdi_uuid
                    credit_note = self.env['account.move'].create({
                        'move_type': 'out_refund',
                        'partner_id': move.partner_id.id,
                        'journal_id': move.journal_id.id,
                        'date': fields.Date.today(),
                        'ref': _('Down Payments of: %s', move.name),
                        'l10n_mx_edi_cfdi_origin': origin,
                        'line_ids': [(0, 0, {
                            'product_id': move.company_id.sale_down_payment_product_id.id,
                            'name': _("Down Payment"),
                            'quantity': 1,
                            'price_unit': down_payment,
                            'tax_ids': [(6, 0, tax_ids)]
                        })]
                    })
                    credit_note.action_post()
        return res
