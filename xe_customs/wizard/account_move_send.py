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
            if move.auto_credit_note:
                move.auto_credit_note = False

                so_line_ids = move.source_orders.order_line.filtered(lambda x: x.invoice_id == move)
                
                down_payment = sum(so_line_ids.mapped('price_unit'))
                if down_payment > 0:
                    tax_ids = move.line_ids.tax_ids.ids
                    origin = False
                    if move.l10n_mx_edi_cfdi_uuid:
                        origin = '07|' + move.l10n_mx_edi_cfdi_uuid
                    credit_note = self.env['account.move'].create({
                        'move_type': 'out_refund',
                        'partner_id': move.partner_id.id,
                        'journal_id': move.journal_id.id,
                        'date': fields.Date.today(),
                        'ref': _('Down Payments of: %s', move.name),
                        'l10n_mx_edi_usage': 'G02',
                        'l10n_mx_edi_cfdi_origin': origin,
                        'l10n_mx_edi_payment_method_id': self.env.ref('l10n_mx_edi.payment_method_anticipos').id or False,
                        'reversal_reason': self.env['account.reversal.reason'].search([('name', '=', 'AMORTIZACIÓN ANTICIPO')], limit=1).id or False,
                        'invoice_line_ids': [(0, 0, {
                            'product_id': move.company_id.sale_down_payment_product_id.id,
                            'name': _("Down Payment"),
                            'quantity': 1,
                            'price_unit': down_payment,
                            'tax_ids': [(6, 0, tax_ids)]
                        })]
                    })
                    credit_note.action_post()
                    credit_note._l10n_mx_edi_cfdi_invoice_try_send()

                    for line in so_line_ids:
                        line.invoice_lines += credit_note.invoice_line_ids

                    # Reconcile
                    for line in credit_note.line_ids.filtered(lambda x: x.reconciled == False).filtered(lambda x: x.account_id.reconcile):
                        move.js_assign_outstanding_line(line.id)
                
        return res
