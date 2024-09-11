# -*- coding: utf-8 -*-
import logging
_logger = logging.getLogger(__name__)

from odoo import api, models, fields, _

class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shipping_assignment = fields.Selection([
        ('logistics', 'Logística'),
        ('shipments', 'Embarques')], store=True, compute="_compute_shipping_assignment")

    kanban_state = fields.Selection([
        ('to_deliver', 'Por entregar'),
        ('delivered', 'Entregado'),
        ('sent', 'Enviado')], default='to_deliver', group_expand='_group_expand_states')

    kanban_task_status = fields.Selection([
        ('to_scheduled', 'ENT POR PROGRAMAR'),
        ('scheduled', 'ENT PROGRAMADA'),
        ('confirmed', 'ENT CONFIRMADA'),
        ('exception', 'ENT EXCEPCIÓN')], default='to_scheduled', group_expand='_group_expand_status')

    @api.depends('x_studio_canal_de_distribucin', 'location_id', 'move_ids')
    def _compute_shipping_assignment(self):
        for rec in self:
            if rec.company_id.id == 4:
                if rec.x_studio_canal_de_distribucin == 'INTERGRUPO':
                    rec.shipping_assignment = 'shipments'
                    continue
                if rec.location_id and rec.location_id.warehouse_id.name:
                    if any(keyword in rec.location_id.warehouse_id.name for keyword in ['Amazon', 'MercadoLibre']):
                        rec.shipping_assignment = 'shipments'
                        continue
                    if rec.location_id.warehouse_id.name == 'Pacific Rim': 
                        if rec.x_studio_canal_de_distribucin == 'DISTRIBUIDORES XE':
                            rec.shipping_assignment = 'shipments'
                            continue
                        if rec.move_ids.filtered(lambda record: record.product_id.default_code == 'FLTENVIO'):
                            rec.shipping_assignment = 'shipments'
                            continue
                rec.shipping_assignment= 'logistics'
                continue
            rec.shipping_assignment = False

    def _group_expand_states(self, states, domain, order):
        return [key for key, val in self._fields['kanban_state'].selection]

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['kanban_task_status'].selection]
