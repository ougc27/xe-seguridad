# -*- coding: utf-8 -*-
#############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author:Anjhana A K(<https://www.cybrosys.com>)
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
from odoo import models, fields, _
from odoo.exceptions import UserError, AccessError


class PosSession(models.Model):
    """Inherited pos session for loading quantity fields from product"""
    _inherit = 'pos.session'

    def _loader_params_product_product(self):
        """Load forcast and on hand quantity field to pos session.
           :return dict: returns dictionary of field parameters for the
                        product model
        """
        result = super()._loader_params_product_product()
        result['search_params']['fields'].append('qty_available')
        result['search_params']['fields'].append('virtual_available')
        return result

    def get_current_cash(self):
        orders = self._get_closed_orders()
        payments = orders.payment_ids.filtered(lambda p: p.payment_method_id.type != "pay_later")
        cash_payment_method_ids = self.payment_method_ids.filtered(lambda pm: pm.type == 'cash')
        default_cash_payment_method_id = cash_payment_method_ids[0] if cash_payment_method_ids else None
        total_default_cash_payment_amount = sum(payments.filtered(lambda p: p.payment_method_id == default_cash_payment_method_id).mapped('amount')) if default_cash_payment_method_id else 0
        last_session = self.search([('config_id', '=', self.config_id.id), ('id', '!=', self.id)], limit=1)
        return (
            last_session.cash_register_balance_end_real
            + total_default_cash_payment_amount
            + sum(self.sudo().statement_line_ids.mapped('amount'))
        )

    def try_cash_in_out(self, _type, amount, reason, extras):
        sign = 1 if _type == 'in' else -1
        sessions = self.filtered('cash_journal_id')

        if not sessions:
            raise UserError(_("There is no cash payment method for this PoS Session"))
        
        current_cash = self.get_current_cash()

        if sign == -1:
            if amount != current_cash:
                raise UserError(_(
                    "The withdrawal amount must match the available cash in the register.\n"
                    "Current available cash: %.2f"
                ) % current_cash)

        self.env['account.bank.statement.line'].sudo().create([
            {
                'pos_session_id': session.id,
                'journal_id': session.cash_journal_id.id,
                'amount': sign * amount,
                'date': fields.Date.context_today(self),
                'payment_ref': '-'.join([session.name, extras['translatedType'], reason]),
            }
            for session in sessions
        ])

    def _validate_session(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        bank_payment_method_diffs = bank_payment_method_diffs or {}
        self.ensure_one()
        data = {}
        sudo = self.user_has_groups('point_of_sale.group_pos_user')
        if self.order_ids.filtered(lambda o: o.state != 'cancel') or self.sudo().statement_line_ids:
            self.cash_real_transaction = sum(self.sudo().statement_line_ids.mapped('amount'))
            if self.state == 'closed':
                raise UserError(_('This session is already closed.'))
            self._check_if_no_draft_orders()
            self._check_invoices_are_posted()
            cash_difference_before_statements = self.cash_register_difference
            if self.update_stock_at_closing:
                self._create_picking_at_end_of_session()
                self._get_closed_orders().filtered(lambda o: not o.is_total_cost_computed)._compute_total_cost_at_session_closing(self.picking_ids.move_ids)
            try:
                with self.env.cr.savepoint():
                    data = self.with_company(self.company_id).with_context(check_move_validity=False, skip_invoice_sync=True)._create_account_move(balancing_account, amount_to_balance, bank_payment_method_diffs)
            except AccessError as e:
                if sudo:
                    data = self.sudo().with_company(self.company_id).with_context(check_move_validity=False, skip_invoice_sync=True)._create_account_move(balancing_account, amount_to_balance, bank_payment_method_diffs)
                else:
                    raise e

            balance = sum(self.move_id.line_ids.mapped('balance'))
            try:
                with self.move_id._check_balanced({'records': self.move_id.sudo()}):
                    pass
            except UserError:
                # Creating the account move is just part of a big database transaction
                # when closing a session. There are other database changes that will happen
                # before attempting to create the account move, such as, creating the picking
                # records.
                # We don't, however, want them to be committed when the account move creation
                # failed; therefore, we need to roll back this transaction before showing the
                # close session wizard.
                self.env.cr.rollback()
                return self._close_session_action(balance)

            self.sudo()._post_statement_difference(cash_difference_before_statements, False)
            if self.move_id.line_ids:
                self.move_id.sudo().with_company(self.company_id)._post()
                #We need to write the price_subtotal and price_total here because if we do it earlier the compute functions will overwrite it here /account/models/account_move_line.py _compute_totals
                for dummy, amount_data in data['sales'].items():
                    self.env['account.move.line'].browse(amount_data['move_line_id']).sudo().with_company(self.company_id).write({
                        'price_subtotal': abs(amount_data['amount_converted']),
                        'price_total': abs(amount_data['amount_converted']) + abs(amount_data['tax_amount']),
                    })
                # Set the uninvoiced orders' state to 'done'
                self.env['pos.order'].search([('session_id', '=', self.id), ('state', '=', 'paid')]).write({'state': 'done'})
            else:
                self.move_id.sudo().unlink()
            self.sudo().with_company(self.company_id)._reconcile_account_move_lines(data)
        else:
            self.sudo()._post_statement_difference(self.cash_register_difference, False)

        self.write({'state': 'closed'})
        return True
