# coding: utf-8

from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def _compute_es_usuario_avanzado_comision(self):
        self.es_usuario_avanzado_comision = self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group') and not self.env.user.has_group('comisiones.comisiones_pago_comisiones_group')

    def _compute_es_administrador_comision(self):
        self.es_administrador_comision = self.env.user.has_group('comisiones.comisiones_administrador_group')

    def _default_es_usuario_avanzado_comision(self):
        return self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group') and not self.env.user.has_group('comisiones.comisiones_pago_comisiones_group')

    def _default_es_administrador_comision(self):
        return self.env.user.has_group('comisiones.comisiones_administrador_group')

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

    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        res['agente1_id'] = self.agente1_id.id
        res['por_agente1'] = self.por_agente1
        res['agente2_id'] = self.agente2_id.id
        res['por_agente2'] = self.por_agente2
        return res

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        sale_order_id = super(SaleOrder, self).copy(default)
        sale_order_id.onchange_partner_id()
        return sale_order_id
