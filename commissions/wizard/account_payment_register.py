# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def action_create_payments(self):
        res = super(AccountPaymentRegister, self).action_create_payments()

        # CASE 1: A PAYMENT IS MADE FROM THE FORM VIEW
        if res and isinstance(res, bool):
            move_id = self.env['account.move.line'].search([
                ('id', 'in', self._context.get('active_ids', [])),
            ]).move_id
            if move_id:
                payment_ids = self.env['account.payment'].search([
                    ('partner_id', '=', move_id.partner_id.id),
                    ('payment_type', '=', 'inbound'),
                ]).filtered(
                    lambda o: move_id.id in o.reconciled_invoice_ids.ids
                )
                if not payment_ids:
                    raise UserError(_('Error in payment creation.'))
                for commission_id in payment_ids.commission_ids:
                    move_id.write({'commission_ids': [(4, commission_id.id)]})
                    commission_id.write({'move_ids': [(4, move_id.id)]})
                    commission_id.get_sales()

        # CASE 2: A PAYMENT IS MADE FROM THE TREE VIEW AND RETURNS 1 PAYMENT
        if res and isinstance(res, dict) and 'res_id' in res:
            payment_id = self.env['account.payment'].browse(res['res_id'])
            for line_id in payment_id.line_ids:
                for matched_debit_id in line_id.matched_debit_ids:
                    move_id = matched_debit_id.debit_move_id.move_id
                    for commission_id in payment_id.commission_ids:
                        move_id.write({'commission_ids': [(4, commission_id.id)]})
                        commission_id.write({'move_ids': [(4, move_id.id)]})
                        commission_id.get_sales()

        # CASE 3: A PAYMENT IS MADE FROM THE TREE VIEW AND RETURNS "N" PAYMENTS
        if res and isinstance(res, dict) and 'domain' in res:
            payment_ids = self.env['account.payment'].browse(res['domain'][0][2])
            for payment_id in payment_ids:
                for line_id in payment_id.line_ids:
                    for matched_debit_id in line_id.matched_debit_ids:
                        move_id = matched_debit_id.debit_move_id.move_id
                        for commission_id in payment_id.commission_ids:
                            move_id.write({'commission_ids': [(4, commission_id.id)]})
                            commission_id.write({'move_ids': [(4, move_id.id)]})
                            commission_id.get_sales()
        return res
