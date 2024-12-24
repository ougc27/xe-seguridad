# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def check_paid_commissions(self, error):
        if self:
            commissions_ids = self.env['xe.commissions'].search([
                ('payment_id', '=', self.id),
                ('paid', '=', True),
            ])
            if commissions_ids:
                raise UserError(error)

    def delete_unpaid_commissions(self):
        if self:
            commissions_ids = self.env['xe.commissions'].search([
                ('payment_id', '=', self.id),
                ('paid', '=', False),
            ])
            if commissions_ids:
                commissions_ids.sudo().unlink()

    def action_draft(self):
        if self:
            error = _('You cannot reset this payment to draft because it has paid commissions.')
            self.check_paid_commissions(error)
        res = super(AccountPayment, self).action_draft()
        if self:
            self.delete_unpaid_commissions()
        return res

    def action_cancel(self):
        if self:
            error = _('You cannot cancel this payment because it has paid commissions.')
            self.check_paid_commissions(error)
        res =  super(AccountPayment, self).action_cancel()
        if self:
            self.delete_unpaid_commissions()
        return res
