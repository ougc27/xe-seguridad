# coding: utf-8

from odoo import api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _compute_es_usuario_avanzado_comision(self):
        self.es_usuario_avanzado_comision = self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group') and not self.env.user.has_group('comisiones.comisiones_pago_comisiones_group')

    def _compute_es_administrador_comision(self):
        self.es_administrador_comision = self.env.user.has_group('comisiones.comisiones_administrador_group')

    def _default_es_usuario_avanzado_comision(self):
        return self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group') and not self.env.user.has_group('comisiones.comisiones_pago_comisiones_group')

    def _default_es_administrador_comision(self):
        return self.env.user.has_group('comisiones.comisiones_administrador_group')

    @api.depends('comision_ids')
    def _compute_ctd_comision(self):
        for record in self:
            record.ctd_comision = len(record.comision_ids)

    agente1_id = fields.Many2one(
        comodel_name='xe.agente',
        string='Agente 1',
    )
    por_agente1 = fields.Float(
        string='Porcentaje agente 1',
        copy=False,
    )
    agente2_id = fields.Many2one(
        comodel_name='xe.agente',
        string='Agente 2',
    )
    por_agente2 = fields.Float(
        string='Porcentaje agente 2',
        copy=False,
    )
    es_usuario_avanzado_comision = fields.Boolean(
        string='Es usuario avanzado comisión',
        compute='_compute_es_usuario_avanzado_comision',
        default=_default_es_usuario_avanzado_comision,
    )
    es_administrador_comision = fields.Boolean(
        string='Es administrador comisión',
        compute='_compute_es_administrador_comision',
        default=_default_es_administrador_comision,
    )
    comision_ids = fields.Many2many(
        comodel_name='xe.comisiones',
        string='Comisiones',
        copy=False,
    )
    ctd_comision = fields.Integer(
        string='Cantidad de comisiones',
        compute='_compute_ctd_comision',
    )

    def action_open_comisiones(self):
        action = self.env['ir.actions.actions']._for_xml_id('comisiones.comisiones_open_action')
        action['domain'] = [('id', 'in', self.comision_ids.ids)]
        return action

    def button_draft(self):
        comision_pagada_ids = self.comision_ids.filtered(
            lambda o: o.pagado
        )
        if comision_pagada_ids:
            raise UserError('No puede restablecer a borrador esta factura debido a que cuenta con comisiones pagadas.')
        return super(AccountMove, self).button_draft()

    def button_cancel(self):
        comision_pagada_ids = self.comision_ids.filtered(
            lambda o: o.pagado
        )
        if comision_pagada_ids:
            raise UserError('No puede cancelar esta factura debido a que cuenta con comisiones pagadas.')
        return super(AccountMove, self).button_cancel()

    def button_request_cancel(self):
        comision_pagada_ids = self.comision_ids.filtered(
            lambda o: o.pagado
        )
        if comision_pagada_ids:
            raise UserError('No puede cancelar esta factura debido a que cuenta con comisiones pagadas.')
        return super(AccountMove, self).button_request_cancel()

    @api.onchange('partner_id')
    def onchange_partner_id(self):
        agente1_id = False
        por_agente1 = 0
        agente2_id = False
        por_agente2 = 0
        if self.partner_id:
            agente1_id = self.partner_id.agente1_id
            por_agente1 = self.partner_id.por_agente1
            agente2_id = self.partner_id.agente2_id
            por_agente2 = self.partner_id.por_agente2
        self.agente1_id = agente1_id
        self.por_agente1 = por_agente1
        self.agente2_id = agente2_id
        self.por_agente2 = por_agente2

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        move_id = super(AccountMove, self).copy(default)
        move_id.onchange_partner_id()
        return move_id

    def js_assign_outstanding_line(self, line_id):
        res = super(AccountMove, self).js_assign_outstanding_line(line_id)
        payment_id = self.env['account.move.line'].browse(line_id).payment_id
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
        payment_id = self.env['account.payment'].browse(id_payment)
        comision_id = self.env['xe.comisiones'].create({
            'posicion': posicion,
            'agente_id': agente,
            'fecha': move_id.invoice_date,
            'move_id': move_id.id,
            'fecha_cobro': payment_id.date,
            'cliente_id': move_id.partner_id.id,
            'cobrado': cobrado,
            'currency_id': payment_id.currency_id.id,
            'comision': comision,
            'pagado': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'por_agente': por_agente,
        })
        move_id.write({
            'comision_ids': [(4, comision_id.id)]
        })