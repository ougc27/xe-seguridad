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
                    ('reconciled_invoice_ids', 'in', move_id.ids)
                ]).sorted(lambda o: o.id, reverse=True)
                if not payment_ids:
                    raise UserError(_('Error in payment creation.'))
                collected = self.amount / 1.16
                if move_id.agent1_id and move_id.agent1_per:
                    agent = move_id.agent1_id.id
                    commission = collected * move_id.agent1_per / 100
                    self.create_commission(agent, move_id, collected, commission, payment_ids[0].id, 1, move_id.agent1_per)
                if move_id.agent2_id and move_id.agent2_per:
                    agent = move_id.agent2_id.id
                    commission = collected * move_id.agent2_per / 100
                    self.create_commission(agent, move_id, collected, commission, payment_ids[0].id, 2, move_id.agent2_per)

        # CASE 2: A PAYMENT IS MADE FROM THE TREE VIEW AND RETURNS 1 PAYMENT
        if res and isinstance(res, dict) and 'res_id' in res:
            payment_id = self.env['account.payment'].browse(res['res_id'])
            for line_id in payment_id.line_ids:
                for matched_debit_id in line_id.matched_debit_ids:
                    move_id = matched_debit_id.debit_move_id.move_id
                    collected = matched_debit_id.amount / 1.16
                    if move_id.agent1_id and move_id.agent1_per:
                        agent = move_id.agent1_id.id
                        commission = collected * move_id.agent1_per / 100
                        self.create_commission(agent, move_id, collected, commission, res['res_id'], 1, move_id.agent1_per)
                    if move_id.agent2_id and move_id.agent2_per:
                        agent = move_id.agent2_id.id
                        commission = collected * move_id.agent2_per / 100
                        self.create_commission(agent, move_id, collected, commission, res['res_id'], 2, move_id.agent2_per)

        # CASE 3: A PAYMENT IS MADE FROM THE TREE VIEW AND RETURNS "N" PAYMENTS
        if res and isinstance(res, dict) and 'domain' in res:
            payment_ids = self.env['account.payment'].browse(res['domain'][0][2])
            for payment_id in payment_ids:
                for line_id in payment_id.line_ids:
                    for matched_debit_id in line_id.matched_debit_ids:
                        move_id = matched_debit_id.debit_move_id.move_id
                        collected = matched_debit_id.amount / 1.16
                        if move_id.agent1_id and move_id.agent1_per:
                            agent = move_id.agent1_id.id
                            commission = collected * move_id.agent1_per / 100
                            self.create_commission(agent, move_id, collected, commission, payment_id.id, 1, move_id.agent1_per)
                        if move_id.agent2_id and move_id.agent2_per:
                            agent = move_id.agent2_id.id
                            commission = collected * move_id.agent2_per / 100
                            self.create_commission(agent, move_id, collected, commission, payment_id.id, 2, move_id.agent2_per)
        return res

    def create_commission(self, agent, move_id, collected, commission, id_payment, position, agent_per):
        commission_id = self.env['xe.commissions'].create({
            'position': position,
            'agent_id': agent,
            'date': move_id.invoice_date,
            'move_id': move_id.id,
            'collected_date': self.payment_date,
            'customer_id': move_id.partner_id.id,
            'collected': collected,
            'currency_id': self.currency_id.id,
            'commission': commission,
            'paid': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'agent_per': agent_per,
        })
        move_id.write({
            'commission_ids': [(4, commission_id.id)]
        })