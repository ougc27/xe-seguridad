# coding: utf-8

from odoo import api, fields, models
from odoo.exceptions import UserError


class XeAgente(models.Model):
    _name = 'xe.agente'
    _description = 'Agente'

    name = fields.Many2one(
        comodel_name='hr.employee',
        string='Agente',
    )
    numero = fields.Integer(
        string='Número de agente',
    )
    comision = fields.Float(
        string='Comisión (%)',
    )

    def validar_data(self):
        if self.numero <= 0:
            raise UserError('El número de agente debe ser mayor a 0.')
        if self.comision <= 0:
            raise UserError('La comisión del agente debe ser mayor a 0.')
        agente_repetido_ids = self.search([
            ('name', '=', self.name.id),
            ('id', '!=', self.id),
        ])
        if agente_repetido_ids:
            raise UserError('Ya existe un registro para el agente {0}.'.format(self.name.name))

    @api.model_create_multi
    def create(self, val_list):
        agente_ids = super(XeAgente, self).create(val_list)
        for agente_id in agente_ids:
            agente_id.validar_data()
        return agente_ids

    def write(self, vals):
        res = super(XeAgente, self).write(vals)
        for agente_id in self:
            agente_id.validar_data()
        return res