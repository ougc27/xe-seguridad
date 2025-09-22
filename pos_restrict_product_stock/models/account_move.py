from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.tools import (
    format_amount,
)
from contextlib import contextmanager


class AccountMove(models.Model):
    _inherit = "account.move"

    pos_session_id = fields.Many2one(
        'pos.session',
        readonly=True
    )

    def action_view_bank_payments(self):
        self.ensure_one()
        payments = self.pos_session_id.bank_payment_ids

        if len(payments) == 1:
            return {
                'name': _('Bank Payment'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.payment',
                'view_mode': 'form',
                'res_id': payments.id,
                'target': 'current',
                'context': {'create': False},
            }
        else:
            return {
                'name': _('Bank Payments'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.payment',
                'view_mode': 'tree,form',
                'domain': [('id', 'in', payments.ids)],
                'target': 'current',
                'context': {'create': False},
            }

    @contextmanager
    def _check_balanced(self, container):
        ''' Assert the move is fully balanced debit = credit.
        An error is raised if it's not the case.
        '''
        with self._disable_recursion(container, 'check_move_validity', default=True, target=False) as disabled:
            yield
            if disabled:
                return

        unbalanced_moves = self._get_unbalanced_moves(container)
        if unbalanced_moves:
            error_msg = _("An error has occurred.")
            for move_id, sum_debit, sum_credit in unbalanced_moves:
                move = self.browse(move_id)
                error_msg += _(
                    "\n\n"
                    "The move (%s) is not balanced.\n"
                    "The total of debits equals %s and the total of credits equals %s.\n"
                    "You might want to specify a default account on journal \"%s\" to automatically balance each move.",
                    move.display_name,
                    format_amount(self.env, sum_debit, move.company_id.currency_id),
                    format_amount(self.env, sum_credit, move.company_id.currency_id),
                    move.journal_id.name)
            raise UserError(error_msg)
