from odoo import api, models, fields, _
from odoo.osv import expression
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    #shipping_assignment = fields.Selection([
        #('logistics', 'Logística'),
        #('shipments', 'Embarques')], store=True, compute="_compute_shipping_assignment")

    kanban_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('confirmed', 'CONFIRMAD0'),
        ('shipped', 'EMBARCADO / RUTA'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_states')

    shipment_task_status = fields.Selection([
        ('to_scheduled', 'POR PROGRAMAR'),
        ('scheduled', 'PROGRAMADO'),
        ('warehouse', 'ALMACÉN'),
        ('production', 'PRODUCCIÓN'),
        ('shipments', 'EMBARQUES'),
        ('confirmed', 'EMBARCADO'),
        ('finished', 'FINALIZADO'),
        ('exception', 'EXCEPCIÓN')], tracking=True, default='to_scheduled', group_expand='_group_expand_status')

    #x_studio_canal_de_distribucin = fields.Char(
        #string="Nombre del Equipo de Ventas",
        #related='group_id.sale_id.partner_id.team_id.name',
        #store=True,
        #readonly=True
    #)

    is_loose_construction = fields.Boolean()

    # @api.depends('location_id', 'move_ids', 'x_studio_canal_de_distribucin')
    # def _compute_shipping_assignment(self):
    #     for rec in self:
    #         if rec.company_id.id == 4:
    #             if rec.x_studio_canal_de_distribucin == 'INTERGRUPO':
    #                 rec.shipping_assignment = 'shipments'
    #                 continue
    #             if rec.location_id and rec.location_id.warehouse_id.name:
    #                 if any(keyword in rec.location_id.warehouse_id.name for keyword in ['Amazon', 'MercadoLibre']):
    #                     rec.shipping_assignment = 'shipments'
    #                     continue
    #                 if rec.location_id.warehouse_id.name == 'Monterrey PR': 
    #                     if rec.x_studio_canal_de_distribucin == 'DISTRIBUIDORES':
    #                         rec.shipping_assignment = 'shipments'
    #                         continue
    #                     if rec.move_ids.filtered(lambda record: record.product_id.default_code == 'FLTENVIO'):
    #                         rec.shipping_assignment = 'shipments'
    #                         continue
    #             rec.shipping_assignment= 'logistics'
    #             continue
    #         else:
    #             rec.shipping_assignment = 'shipments'

    def _group_expand_states(self, states, domain, order):
        return [key for key, val in self._fields['kanban_task_status'].selection]

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['shipment_task_status'].selection]
