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


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _pos_needs_price_include(self):
        """Durable check (independent of any transient context): True when this
        invoice/credit-note line comes from a POS order priced under the 'IVA
        incluido en POS' scheme. In that case the tax must be computed backwards
        (the price already includes tax) on EVERY (re)computation -- not only at
        creation time -- otherwise reopening/posting/editing the invoice makes
        Odoo recompute the tax 'excluded' (forward) and it adds the IVA twice.
        """
        self.ensure_one()
        if self.move_id.move_type not in ('out_invoice', 'out_refund'):
            return False
        order = self.move_id.pos_order_ids[:1]
        if not order or not order.config_id.tax_id.pos_price_include:
            return False
        if not order._pos_is_tax_included_order():
            return False
        return bool(self.tax_ids.filtered(lambda t: t.pos_price_include))

    def _compute_all_tax(self):
        # Durable POS 'IVA incluido' computation: the stored tax lines / move
        # totals come from here (it calls line.tax_ids.compute_all directly).
        # For POS included lines we force the native 'force_price_include' so
        # the tax is computed backwards on EVERY recompute (create, post, open,
        # edit), never adding the IVA twice.
        incl = self.filtered(lambda l: l._pos_needs_price_include())
        if incl:
            super(AccountMoveLine, incl.with_context(force_price_include=True))._compute_all_tax()
        rest = self - incl
        if rest:
            super(AccountMoveLine, rest)._compute_all_tax()

    def _compute_totals(self):
        # Same durable treatment for the line subtotal/total (price_subtotal,
        # price_total) which also call tax_ids.compute_all directly.
        incl = self.filtered(lambda l: l._pos_needs_price_include())
        if incl:
            super(AccountMoveLine, incl.with_context(force_price_include=True))._compute_totals()
        rest = self - incl
        if rest:
            super(AccountMoveLine, rest)._compute_totals()

    def _convert_to_tax_base_line_dict(self):
        res = super()._convert_to_tax_base_line_dict()
        if self._pos_needs_price_include():
            res['extra_context'] = {
                **(res.get('extra_context') or {}),
                'force_price_include': True,
            }
        return res
