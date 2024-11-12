# -*- coding: utf-8 -*-
import logging
_logger = logging.getLogger(__name__)

from odoo import api, models, fields, _
from odoo.osv import expression


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    #, compute="_compute_shipping_assignment"

    shipping_assignment = fields.Selection([
        ('logistics', 'Logística'),
        ('shipments', 'Embarques')], store=True, compute="_compute_shipping_assignment")

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

    x_studio_canal_de_distribucin = fields.Char(
        string="Nombre del Equipo de Ventas",
        related='group_id.sale_id.partner_id.team_id.name',
        store=True,
        readonly=True
    )

    is_remission_separated = fields.Boolean()

    x_studio_folio_rem = fields.Char(string='Remisión')

    end_date = fields.Datetime(
        index=True, tracking=True)

    partner_id = fields.Many2one(
        'res.partner', 'Contact',
        check_company=True, index='btree_not_null', tracking=True)

    is_loose_construction = fields.Boolean()

    tag_ids = fields.Many2many(
        'inventory.tag', 'inventory_tag_rel', 'picking_id', 'tag_id', string='Tags')

    @api.depends('location_id', 'move_ids', 'x_studio_canal_de_distribucin')
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
                    if rec.location_id.warehouse_id.name == 'Monterrey PR': 
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
        return [key for key, val in self._fields['kanban_task_status'].selection]

    def _group_expand_status(self, states, domain, order):
        return [key for key, val in self._fields['shipment_task_status'].selection]

    def separate_remissions(self):
        first_move_id = self.move_ids[0].id
        scheduled_date = self.scheduled_date
        self.write({'is_remission_separated': True})
        for move in self.move_ids:
            quantity = int(move.product_uom_qty)
            if move.id == first_move_id:
                quantity -= 1
            if quantity >= 1:
                move.write({'product_uom_qty': 1})
            for i in range(quantity):
                new_picking = self.copy(
                    {
                        'is_remission_separated': True,
                        'scheduled_date': scheduled_date, 
                    }
                )
                for new_move in new_picking.move_ids:
                    if new_move.product_id.id == move.product_id.id:
                        new_move.write({'product_uom_qty': 1})
                        continue
                    new_move.unlink()

                new_picking.write({
                    'state': 'confirmed'
                })
                #new_picking.action_confirm()
        for move in self.move_ids:
            if move.id != first_move_id:
                move.unlink()
        #self.action_confirm()

    @api.depends('name', 'x_studio_folio_rem')
    @api.depends_context('from_helpdesk_ticket')
    def _compute_display_name(self):
        if not self._context.get('from_helpdesk_ticket'):
            return super()._compute_display_name()
        for rec in self:
            name = rec.name
            if rec.x_studio_folio_rem:
                name = f'{name} - {rec.x_studio_folio_rem}'
            rec.display_name = name

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        if self.env.context.get('from_helpdesk_ticket'):
            query = """
                SELECT id
                    FROM stock_picking
                    WHERE
                    (
                        name ilike %s
                    OR
                        x_studio_folio_rem ilike %s
                    )
                    AND
                        company_id = %s
                    AND
	                    TRIM(name) != ''
                    AND 
                        TRIM(x_studio_folio_rem) != ''
                    LIMIT %s
            """
            self.env.cr.execute(
                query, (
                    '%' + name + '%',
                    '%' + name + '%',
                    self.env.company.id,
                    limit
                )
            )
            return [row[0] for row in self.env.cr.fetchall()]
        return super()._name_search(name, domain, operator, limit, order)
