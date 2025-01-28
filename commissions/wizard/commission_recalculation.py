# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import UserError


class CommissionRecalculation(models.TransientModel):
    _name = 'commission.recalculation'
    _description = 'Commission recalculation'

    start_date = fields.Date(
        string='Start date',
    )
    end_date = fields.Date(
        string='End date',
    )
    agent_ids = fields.Many2many(
        comodel_name='xe.agent',
        string='Agents',
    )

    def action_commission_recalculation(self):
        move_ids = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', self.start_date),
            ('invoice_date', '<=', self.end_date),
            ('state', '=', 'posted'),
            ('exclude_recalculation', '=', False),
        ])

        ids_agent = self.agent_ids.ids
        for move_id in move_ids:
            agent_check_ids = self.env['xe.agent']
            if move_id.agent1_id:
                agent_check_ids |= move_id.agent1_id
            if move_id.agent2_id:
                agent_check_ids |= move_id.agent2_id
            if move_id.partner_id.agent1_id:
                agent_check_ids |= move_id.partner_id.agent1_id
            if move_id.partner_id.agent2_id:
                agent_check_ids |= move_id.partner_id.agent2_id
            ids_agent_check = agent_check_ids.ids
            ids_agent_check = set(ids_agent) & set(ids_agent_check)
            if ids_agent_check:
                for id_agent_check in ids_agent_check:

                    # AGENT 1
                    if move_id.agent1_id.id == id_agent_check or move_id.partner_id.agent1_id.id == id_agent_check:
                        if move_id.agent1_id != move_id.partner_id.agent1_id or move_id.agent1_per != move_id.partner_id.agent1_per:
                            move_id.write({
                                'agent1_id': move_id.partner_id.agent1_id.id,
                                'agent1_per': move_id.partner_id.agent1_per,
                            })

                        move_id.line_ids.sale_line_ids.order_id.write({
                            'agent1_id': move_id.partner_id.agent1_id.id,
                            'agent1_per': move_id.partner_id.agent1_per,
                        })

                        reconciled_lines = move_id.line_ids.mapped('matched_credit_ids') + move_id.line_ids.mapped('matched_debit_ids')
                        payment_lines = reconciled_lines.mapped('credit_move_id') + reconciled_lines.mapped('debit_move_id')
                        payment_ids = payment_lines.mapped('payment_id')

                        for payment_id in payment_ids:
                            commission_ids = move_id.commission_ids.filtered(
                                lambda o: o.payment_id == payment_id and
                                          o.position == 1
                            )
                            if commission_ids:
                                unpaid_commission_ids = commission_ids.filtered(
                                    lambda o: not o.paid
                                )
                                if unpaid_commission_ids:
                                    if move_id.agent1_id:
                                        for unpaid_commission_id in unpaid_commission_ids:
                                            if unpaid_commission_id.agent_id != move_id.agent1_id or unpaid_commission_id.agent_per != move_id.agent1_per:
                                                unpaid_commission_id.write({
                                                    'agent_id': move_id.agent1_id.id,
                                                    'commission': unpaid_commission_id.collected * move_id.agent1_per / 100,
                                                    'agent_per': move_id.agent1_per,
                                                })
                                    else:
                                        unpaid_commission_ids.unlink()
                            else:
                                for line_id in payment_id.line_ids:
                                    for matched_debit_id in line_id.matched_debit_ids:
                                        collected = matched_debit_id.amount / 1.16
                                        if move_id.agent1_id and move_id.agent1_per:
                                            agent = move_id.agent1_id.id
                                            commission = collected * move_id.agent1_per / 100
                                            self.create_commission(agent, move_id, collected, commission, payment_id.id, 1, move_id.agent1_per)

                    # AGENT 2
                    if move_id.agent2_id.id == id_agent_check or move_id.partner_id.agent2_id.id == id_agent_check:
                        if move_id.agent2_id != move_id.partner_id.agent2_id or move_id.agent2_per != move_id.partner_id.agent2_per:
                            move_id.write({
                                'agent2_id': move_id.partner_id.agent2_id.id,
                                'agent2_per': move_id.partner_id.agent2_per,
                            })

                        move_id.line_ids.sale_line_ids.order_id.write({
                            'agent2_id': move_id.partner_id.agent2_id.id,
                            'agent2_per': move_id.partner_id.agent2_per,
                        })

                        reconciled_lines = move_id.line_ids.mapped('matched_credit_ids') + move_id.line_ids.mapped('matched_debit_ids')
                        payment_lines = reconciled_lines.mapped('credit_move_id') + reconciled_lines.mapped('debit_move_id')
                        payment_ids = payment_lines.mapped('payment_id')

                        for payment_id in payment_ids:
                            commission_ids = move_id.commission_ids.filtered(
                                lambda o: o.payment_id == payment_id and
                                          o.position == 2
                            )
                            if commission_ids:
                                unpaid_commission_ids = commission_ids.filtered(
                                    lambda o: not o.paid
                                )
                                if unpaid_commission_ids:
                                    if move_id.agent2_id:
                                        for unpaid_commission_id in unpaid_commission_ids:
                                            if unpaid_commission_id.agent_id != move_id.agent2_id or unpaid_commission_id.agent_per != move_id.agent2_per:
                                                unpaid_commission_id.write({
                                                    'agent_id': move_id.agent2_id.id,
                                                    'commission': unpaid_commission_id.collected * move_id.agent2_per / 100,
                                                    'agent_per': move_id.agent2_per,
                                                })
                                    else:
                                        unpaid_commission_ids.unlink()
                            else:
                                for line_id in payment_id.line_ids:
                                    for matched_debit_id in line_id.matched_debit_ids:
                                        collected = matched_debit_id.amount / 1.16
                                        if move_id.agent2_id and move_id.agent2_per:
                                            agent = move_id.agent2_id.id
                                            commission = collected * move_id.agent2_per / 100
                                            self.create_commission(agent, move_id, collected, commission, payment_id.id, 2, move_id.agent2_per)

    def create_commission(self, id_agent, move_id, collected, commission, id_payment, position, agent_per):
        payment_id = self.env['account.payment'].browse(id_payment)
        commission_id = self.env['xe.commissions'].create({
            'position': position,
            'agent_id': id_agent,
            'date': move_id.invoice_date,
            'move_id': move_id.id,
            'collected_date': payment_id.date,
            'customer_id': move_id.partner_id.id,
            'collected': collected,
            'currency_id': payment_id.currency_id.id,
            'commission': commission,
            'paid': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'agent_per': agent_per,
        })
        move_id.write({
            'commission_ids': [(4, commission_id.id)]
        })
