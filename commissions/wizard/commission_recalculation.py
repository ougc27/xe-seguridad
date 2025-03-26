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
        payment_ids = self.env['account.payment'].search([
            ('payment_type', '=', 'inbound'),
            ('journal_id.generates_commission', '=', True),
            ('destination_account_id.generates_commission', '=', True),
            ('date', '>=', self.start_date),
            ('date', '<=', self.end_date),
            ('state', '=', 'posted'),
            ('exclude_recalculation', '=', False),
        ])

        ids_agent = self.agent_ids.ids
        for payment_id in payment_ids:
            agent_check_ids = self.env['xe.agent']
            if payment_id.agent1_id:
                agent_check_ids |= payment_id.agent1_id
            if payment_id.agent2_id:
                agent_check_ids |= payment_id.agent2_id
            if payment_id.partner_id.agent1_id:
                agent_check_ids |= payment_id.partner_id.agent1_id
            if payment_id.partner_id.agent2_id:
                agent_check_ids |= payment_id.partner_id.agent2_id
            ids_agent_check = agent_check_ids.ids
            ids_agent_check = set(ids_agent) & set(ids_agent_check)
            if ids_agent_check:
                for id_agent_check in ids_agent_check:

                    # AGENT 1
                    if payment_id.agent1_id.id == id_agent_check or payment_id.partner_id.agent1_id.id == id_agent_check:
                        if payment_id.agent1_id != payment_id.partner_id.agent1_id or payment_id.agent1_per != payment_id.partner_id.agent1_per:
                            payment_id.write({
                                'agent1_id': payment_id.partner_id.agent1_id.id,
                                'agent1_per': payment_id.partner_id.agent1_per,
                            })

                        commission_id = payment_id.commission_ids.filtered(
                            lambda o: o.payment_id == payment_id and
                                      o.position == 1
                        )
                        if commission_id:
                            if not commission_id.paid:
                                if payment_id.agent1_id:
                                    if commission_id.agent_id != payment_id.agent1_id or commission_id.agent_per != payment_id.agent1_per:
                                        if payment_id.partner_id.is_border_zone_iva:
                                            collected = payment_id.amount / 1.08
                                        else:
                                            collected = payment_id.amount / 1.16
                                        commission_id.write({
                                            'agent_id': payment_id.agent1_id.id,
                                            'commission': collected * payment_id.agent1_per / 100,
                                            'agent_per': payment_id.agent1_per,
                                        })
                                else:
                                    commission_id.unlink()
                        else:
                            if (
                                payment_id.journal_id.generates_commission and
                                payment_id.destination_account_id.generates_commission
                            ):
                                if payment_id.partner_id.is_border_zone_iva:
                                    collected = payment_id.amount / 1.08
                                else:
                                    collected = payment_id.amount / 1.16
                                if payment_id.agent1_id and payment_id.agent1_per:
                                    agent = payment_id.agent1_id.id
                                    commission = collected * payment_id.agent1_per / 100
                                    new_commission_id = self.create_commission(agent, payment_id, collected, commission, payment_id.reconciled_invoice_ids.ids, 1, payment_id.agent1_per)
                                    new_commission_id.get_sales()

                    # AGENT 2
                    if payment_id.agent2_id.id == id_agent_check or payment_id.partner_id.agent2_id.id == id_agent_check:
                        if payment_id.agent2_id != payment_id.partner_id.agent2_id or payment_id.agent2_per != payment_id.partner_id.agent2_per:
                            payment_id.write({
                                'agent2_id': payment_id.partner_id.agent2_id.id,
                                'agent2_per': payment_id.partner_id.agent2_per,
                            })

                        commission_id = payment_id.commission_ids.filtered(
                            lambda o: o.payment_id == payment_id and
                                      o.position == 2
                        )
                        if commission_id:
                            if not commission_id.paid:
                                if payment_id.agent2_id:
                                    if commission_id.agent_id != payment_id.agent2_id or commission_id.agent_per != payment_id.agent2_per:
                                        if payment_id.partner_id.is_border_zone_iva:
                                            collected = payment_id.amount / 1.08
                                        else:
                                            collected = payment_id.amount / 1.16
                                        commission_id.write({
                                            'agent_id': payment_id.agent2_id.id,
                                            'commission': collected * payment_id.agent2_per / 100,
                                            'agent_per': payment_id.agent2_per,
                                        })
                                else:
                                    commission_id.unlink()
                        else:
                            if (
                                payment_id.journal_id.generates_commission and
                                payment_id.destination_account_id.generates_commission
                            ):
                                if payment_id.partner_id.is_border_zone_iva:
                                    collected = payment_id.amount / 1.08
                                else:
                                    collected = payment_id.amount / 1.16
                                if payment_id.agent2_id and payment_id.agent2_per:
                                    agent = payment_id.agent2_id.id
                                    commission = collected * payment_id.agent2_per / 100
                                    new_commission_id = self.create_commission(agent, payment_id, collected, commission, payment_id.reconciled_invoice_ids.ids, 2, payment_id.agent2_per)
                                    new_commission_id.get_sales()

        move_ids = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', self.start_date),
            ('invoice_date', '<=', self.end_date),
            ('state', '=', 'posted'),
            ('exclude_recalculation', '=', False),
            ('pos_originated', '=', True),
        ])

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

                        commission_ids = move_id.commission_ids.filtered(
                            lambda o: o.position == 1
                        )
                        if commission_ids:
                            unpaid_commission_ids = commission_ids.filtered(lambda o: not o.paid)
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
                            collected = move_id.amount_untaxed
                            commission = collected * move_id.agent1_per / 100
                            pos_order_id = self.env['pos.order'].search([
                                ('name', '=', move_id.invoice_origin)
                            ], limit=1)
                            commissions_id = self.env['xe.commissions'].create({
                                'position': 1,
                                'agent_id': move_id.agent1_id.id if move_id.agent1_id else False,
                                'collected_date': move_id.date,
                                'customer_id': move_id.partner_id.id,
                                'collected': collected,
                                'currency_id': move_id.currency_id.id,
                                'commission': commission,
                                'paid': False,
                                'move_ids': [(4, move_id.id)],
                                'pos_order_id': pos_order_id.id if pos_order_id else False,
                                'agent_per': move_id.agent1_per,
                            })
                            move_id.commission_ids = [(4, commissions_id.id)]

                    # AGENT 2
                    if move_id.agent2_id.id == id_agent_check or move_id.partner_id.agent2_id.id == id_agent_check:
                        if move_id.agent2_id != move_id.partner_id.agent2_id or move_id.agent2_per != move_id.partner_id.agent2_per:
                            move_id.write({
                                'agent2_id': move_id.partner_id.agent2_id.id,
                                'agent2_per': move_id.partner_id.agent2_per,
                            })

                        commission_ids = move_id.commission_ids.filtered(
                            lambda o: o.position == 2
                        )
                        if commission_ids:
                            unpaid_commission_ids = commission_ids.filtered(lambda o: not o.paid)
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
                            collected = move_id.amount_untaxed
                            commission = collected * move_id.agent2_per / 100
                            pos_order_id = self.env['pos.order'].search([
                                ('name', '=', move_id.invoice_origin)
                            ], limit=1)
                            commissions_id = self.env['xe.commissions'].create({
                                'position': 2,
                                'agent_id': move_id.agent2_id.id if move_id.agent2_id else False,
                                'collected_date': move_id.date,
                                'customer_id': move_id.partner_id.id,
                                'collected': collected,
                                'currency_id': move_id.currency_id.id,
                                'commission': commission,
                                'paid': False,
                                'move_ids': [(4, move_id.id)],
                                'pos_order_id': pos_order_id.id if pos_order_id else False,
                                'agent_per': move_id.agent2_per,
                            })
                            move_id.commission_ids = [(4, commissions_id.id)]

    def create_commission(self, id_agent, payment_id, collected, commission, ids_invoices, position, agent_per):
        commission_id = self.env['xe.commissions'].create({
            'position': position,
            'agent_id': id_agent,
            'move_ids': [(6, 0, ids_invoices)],
            'collected_date': payment_id.date,
            'customer_id': payment_id.partner_id.id,
            'collected': collected,
            'currency_id': payment_id.currency_id.id,
            'commission': commission,
            'paid': False,
            'payment_id': payment_id.id,
            'agent_per': agent_per,
        })
        payment_id.write({'commission_ids': [(4, commission_id.id)]})
        move_ids = self.env['account.move'].browse(ids_invoices)
        for move_id in move_ids:
            move_id.write({'commission_ids': [(4, commission_id.id)]})
        return commission_id
