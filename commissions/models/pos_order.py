# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def _prepare_invoice_vals(self):
        vals = super(PosOrder, self)._prepare_invoice_vals()
        vals['pos_originated'] = True
        if 'partner_id' in vals and vals['partner_id']:
            partner_id = self.env['res.partner'].browse(vals['partner_id'])
            vals['agent1_id'] = partner_id.agent1_id.id
            vals['agent1_per'] = partner_id.agent1_per
            vals['agent2_id'] = partner_id.agent2_id.id
            vals['agent2_per'] = partner_id.agent2_per
        return vals

    def _apply_invoice_payments(self, is_reverse=False):
        move_ids = super(PosOrder, self)._apply_invoice_payments(is_reverse)
        invoice_id = self.account_move
        debit_line_id = move_ids.line_ids.filtered(
            lambda o: o.debit
        )
        if debit_line_id:
            debit_line_id = debit_line_id[0]
        destination_account_id = debit_line_id.account_id
        data = []
        if (
                debit_line_id and
                move_ids.journal_id.generates_commission and
                destination_account_id.generates_commission
        ):
            collected = invoice_id.amount_untaxed
            if invoice_id.agent1_id and invoice_id.agent1_per:
                commission = collected * invoice_id.agent1_per / 100
                data.append(
                    {
                        'position': 1,
                        'agent_id': invoice_id.agent1_id.id if invoice_id.agent1_id else False,
                        'collected_date': invoice_id.date,
                        'customer_id': invoice_id.partner_id.id,
                        'collected': collected,
                        'currency_id': invoice_id.currency_id.id,
                        'commission': commission,
                        'paid': False,
                        'move_ids': [(4, invoice_id.id)],
                        'pos_order_id': self.id,
                        'agent_per': invoice_id.agent1_per,
                    }
                )
            if invoice_id.agent2_id and invoice_id.agent2_per:
                commission = collected * invoice_id.agent2_per / 100
                data.append(
                    {
                        'position': 2,
                        'agent_id': invoice_id.agent2_id.id if invoice_id.agent2_id else False,
                        'collected_date': invoice_id.date,
                        'customer_id': invoice_id.partner_id.id,
                        'collected': collected,
                        'currency_id': invoice_id.currency_id.id,
                        'commission': commission,
                        'paid': False,
                        'move_ids': [(4, invoice_id.id)],
                        'pos_order_id': self.id,
                        'agent_per': invoice_id.agent2_per,
                    }
                )
        if data:
            commissions_ids = self.env['xe.commissions'].create(data)
            invoice_id.commission_ids = [(6, 0, commissions_ids.ids)]
        return move_ids
