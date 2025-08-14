from odoo import fields, models, _


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
