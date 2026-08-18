from odoo import api, fields, models, _


class AccountMove(models.Model):
    _inherit = "account.move"

    pos_session_id = fields.Many2one(
        'pos.session',
        readonly=True
    )

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        moves.filtered(lambda m: m.reversed_entry_id)._sync_pos_order_invoice_history()
        return moves

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals or 'reversed_entry_id' in vals:
            self._sync_pos_order_invoice_history()
        return res

    def _sync_pos_order_invoice_history(self):
        """Keep pos.order.invoice_history_ids up to date for moves that are
        not created through the POS invoicing flow directly (e.g. a credit
        note or a cancellation made from Accounting).

        - A credit note (or any reversal) whose reversed_entry_id is already
          part of a pos.order's invoice history gets added to that same
          history.
        - A move that is cancelled and is already part of a pos.order's
          invoice history stays tracked (no-op, it's already there), this
          just guarantees consistency if the state check needs to run again.
        """
        PosOrder = self.env['pos.order'].sudo()
        for move in self:
            related_orders = PosOrder.browse()
            if move.reversed_entry_id:
                related_orders |= PosOrder.search([
                    ('invoice_history_ids', 'in', move.reversed_entry_id.ids)
                ])
            if move.state == 'cancel':
                related_orders |= PosOrder.search([
                    ('invoice_history_ids', 'in', move.ids)
                ])
            for order in related_orders:
                order._add_to_invoice_history(move)

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
