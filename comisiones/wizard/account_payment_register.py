# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import UserError


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def action_create_payments(self):
        res = super(AccountPaymentRegister, self).action_create_payments()

        # CASO 1: SE HACE UN PAGO DESDE LA VISTA FORM
        if res and isinstance(res, bool):
            move_id = self.env['account.move.line'].search([
                ('id', 'in', self._context.get('active_ids', [])),
            ]).move_id
            if move_id:
                payment_ids = self.env['account.payment'].search([
                    ('reconciled_invoice_ids', 'in', move_id.ids)
                ]).sorted(lambda o: o.id, reverse=True)
                if not payment_ids:
                    raise UserError('Error en la creación del pago.')
                cobrado = self.amount / 1.16
                if move_id.agente1_id and move_id.por_agente1:
                    agente = move_id.agente1_id.id
                    comision = cobrado * move_id.por_agente1 / 100
                    self.crear_comision(agente, move_id, cobrado, comision, payment_ids[0].id, 1, move_id.por_agente1)
                if move_id.agente2_id and move_id.por_agente2:
                    agente = move_id.agente2_id.id
                    comision = cobrado * move_id.por_agente2 / 100
                    self.crear_comision(agente, move_id, cobrado, comision, payment_ids[0].id, 2, move_id.por_agente2)

        # CASO 2: SE HACE UN PAGO DESDE LA VISTA TREE Y DEVUELVE 1 PAGO
        if res and isinstance(res, dict) and 'res_id' in res:
            payment_id = self.env['account.payment'].browse(res['res_id'])
            for line_id in payment_id.line_ids:
                for matched_debit_id in line_id.matched_debit_ids:
                    move_id = matched_debit_id.debit_move_id.move_id
                    cobrado = matched_debit_id.amount / 1.16
                    if move_id.agente1_id and move_id.por_agente1:
                        agente = move_id.agente1_id.id
                        comision = cobrado * move_id.por_agente1 / 100
                        self.crear_comision(agente, move_id, cobrado, comision, res['res_id'], 1, move_id.por_agente1)
                    if move_id.agente2_id and move_id.por_agente2:
                        agente = move_id.agente2_id.id
                        comision = cobrado * move_id.por_agente2 / 100
                        self.crear_comision(agente, move_id, cobrado, comision, res['res_id'], 2, move_id.por_agente2)

        # CASO 3: SE HACE UN PAGO DESDE LA VISTA TREE Y DEVUELVE "N" PAGOS
        if res and isinstance(res, dict) and 'domain' in res:
            payment_ids = self.env['account.payment'].browse(res['domain'][0][2])
            for payment_id in payment_ids:
                for line_id in payment_id.line_ids:
                    for matched_debit_id in line_id.matched_debit_ids:
                        move_id = matched_debit_id.debit_move_id.move_id
                        cobrado = matched_debit_id.amount / 1.16
                        if move_id.agente1_id and move_id.por_agente1:
                            agente = move_id.agente1_id.id
                            comision = cobrado * move_id.por_agente1 / 100
                            self.crear_comision(agente, move_id, cobrado, comision, payment_id.id, 1, move_id.por_agente1)
                        if move_id.agente2_id and move_id.por_agente2:
                            agente = move_id.agente2_id.id
                            comision = cobrado * move_id.por_agente2 / 100
                            self.crear_comision(agente, move_id, cobrado, comision, payment_id.id, 2, move_id.por_agente2)
        return res

    def crear_comision(self, agente, move_id, cobrado, comision, id_payment, posicion, por_agente):
        comision_id = self.env['xe.comisiones'].create({
            'posicion': posicion,
            'agente_id': agente,
            'fecha': move_id.invoice_date,
            'move_id': move_id.id,
            'fecha_cobro': self.payment_date,
            'cliente_id': move_id.partner_id.id,
            'cobrado': cobrado,
            'currency_id': self.currency_id.id,
            'comision': comision,
            'pagado': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'por_agente': por_agente,
        })
        move_id.write({
            'comision_ids': [(4, comision_id.id)]
        })