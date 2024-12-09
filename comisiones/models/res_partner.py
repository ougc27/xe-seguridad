# coding: utf-8

from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def _compute_es_usuario_comision(self):
        self.es_usuario_comision = self.env.user.has_group('comisiones.comisiones_usuario_group') and not self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group')

    def _compute_es_usuario_avanzado_comision(self):
        self.es_usuario_avanzado_comision = self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group') and not self.env.user.has_group('comisiones.comisiones_pago_comisiones_group')

    def _compute_es_administrador_comision(self):
        self.es_administrador_comision = self.env.user.has_group('comisiones.comisiones_administrador_group')

    def _default_es_usuario_comision(self):
        return self.env.user.has_group('comisiones.comisiones_usuario_group') and not self.env.user.has_group('comisiones.comisiones_usuario_avanzado_group')

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
    es_usuario_comision = fields.Boolean(
        string='Es usuario comisión',
        compute='_compute_es_usuario_comision',
        default=_default_es_usuario_comision,
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

    @api.onchange('agente1_id')
    def _onchange_agente1_id(self):
        por_agente1 = 0
        if self.agente1_id:
            por_agente1 = self.agente1_id.comision
        self.por_agente1 = por_agente1

    @api.onchange('agente2_id')
    def _onchange_agente2_id(self):
        por_agente2 = 0
        if self.agente2_id:
            por_agente2 = self.agente2_id.comision
        self.por_agente2 = por_agente2