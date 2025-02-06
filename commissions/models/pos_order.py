# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = "pos.order"

    def _generate_pos_order_invoice(self):
        moves = self.env['account.move']

        for order in self:
            # Force company for all SUPERUSER_ID action
            if order.account_move:
                moves += order.account_move
                continue

            if not order.partner_id:
                raise UserError(_('Please provide a partner for the sale.'))

            move_vals = order._prepare_invoice_vals()
            new_move = order._create_invoice(move_vals)

            order.write({'account_move': new_move.id, 'state': 'invoiced'})

            if new_move.partner_id.id in [105377, 105628]:
                new_move.onchange_partner_id()

            new_move.sudo().with_company(order.company_id).with_context(skip_invoice_sync=True)._post()

            if new_move.partner_id.id in [105377, 105628]:
                if new_move.agent1_id and new_move.agent1_per:
                    agent = new_move.agent1_id.id
                    commission = new_move.amount_untaxed * new_move.agent1_per / 100
                    commission_id = self.env['xe.commissions'].create({
                        'position': 1,
                        'agent_id': agent,
                        'date': new_move.invoice_date,
                        'move_id': new_move.id,
                        'collected_date': new_move.invoice_date,
                        'customer_id': new_move.partner_id.id,
                        'collected': new_move.amount_untaxed,
                        'currency_id': new_move.currency_id.id,
                        'commission': commission,
                        'paid': False,
                        'sale_order_ids': False,
                        'payment_id': False,
                        'agent_per': new_move.agent1_per,
                    })
                    new_move.write({
                        'commission_ids': [(4, commission_id.id)]
                    })
                if new_move.agent2_id and new_move.agent2_per:
                    agent = new_move.agent2_id.id
                    commission = new_move.amount_untaxed * new_move.agent2_per / 100
                    commission_id = self.env['xe.commissions'].create({
                        'position': 2,
                        'agent_id': agent,
                        'date': new_move.invoice_date,
                        'move_id': new_move.id,
                        'collected_date': new_move.invoice_date,
                        'customer_id': new_move.partner_id.id,
                        'collected': new_move.amount_untaxed,
                        'currency_id': new_move.currency_id.id,
                        'commission': commission,
                        'paid': False,
                        'sale_order_ids': False,
                        'payment_id': False,
                        'agent_per': new_move.agent2_per,
                    })
                    new_move.write({
                        'commission_ids': [(4, commission_id.id)]
                    })

            moves += new_move
            payment_moves = order._apply_invoice_payments(order.session_id.state == 'closed')

            # Send and Print
            if self.env.context.get('generate_pdf', True):
                template = self.env.ref(new_move._get_mail_template())
                new_move.with_context(skip_invoice_sync=True)._generate_pdf_and_send_invoice(template)


            if order.session_id.state == 'closed':  # If the session isn't closed this isn't needed.
                # If a client requires the invoice later, we need to revers the amount from the closing entry, by making a new entry for that.
                order._create_misc_reversal_move(payment_moves)

        if not moves:
            return {}

        return {
            'name': _('Customer Invoice'),
            'view_mode': 'form',
            'view_id': self.env.ref('account.view_move_form').id,
            'res_model': 'account.move',
            'context': "{'move_type':'out_invoice'}",
            'type': 'ir.actions.act_window',
            'target': 'current',
            'res_id': moves and moves.ids[0] or False,
        }
