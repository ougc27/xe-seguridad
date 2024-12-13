# coding: utf-8

from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def revisar_comisiones_pagadas(self, error):
        if self:
            comisiones_ids = self.env['xe.comisiones'].search([
                ('payment_id', '=', self.id),
                ('pagado', '=', True),
            ])
            if comisiones_ids:
                raise UserError(error)

    def borrar_comisiones_no_pagadas(self):
        if self:
            comisiones_ids = self.env['xe.comisiones'].search([
                ('payment_id', '=', self.id),
                ('pagado', '=', False),
            ])
            if comisiones_ids:
                comisiones_ids.sudo().unlink()

    def action_draft(self):
        if self:
            error = 'No puede restablecer a borrador este pago debido a que cuenta con comisiones pagadas.'
            self.revisar_comisiones_pagadas(error)
        res = super(AccountPayment, self).action_draft()
        if self:
            self.borrar_comisiones_no_pagadas()
        return res

    def action_cancel(self):
        if self:
            error = 'No puede cancelar este pago debido a que cuenta con comisiones pagadas.'
            self.revisar_comisiones_pagadas(error)
        res =  super(AccountPayment, self).action_cancel()
        if self:
            self.borrar_comisiones_no_pagadas()
        return res
