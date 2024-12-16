from odoo import api, models, fields, _
from odoo.osv import expression
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    shipment_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('warehouse', 'ALMACÉN'),
        ('production', 'PRODUCCIÓN'),
        ('shipments', 'EMBARQUES'),
        ('confirmed', 'EMBARCADO'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_status')

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['shipment_task_status'].selection]
