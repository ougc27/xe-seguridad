# coding: utf-8

from odoo import api, fields, models


class XeComisiones(models.Model):
    _name = 'xe.comisiones'
    _description = 'Comisiones'
    _order = 'cliente_id, fecha_cobro'

    @api.depends('sale_order_ids')
    def _compute_sale_orders(self):
        for record in self:
            record.sale_orders = ','.join(record.sale_order_ids.mapped('name'))

    posicion = fields.Integer(
        string='Posición',
    )
    agente_id = fields.Many2one(
        comodel_name='xe.agente',
        string='Agente',
    )
    fecha = fields.Date(
        string='Fecha',
    )
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Folio',
    )
    fecha_cobro = fields.Date(
        string='Fecha cobro',
    )
    cliente_id = fields.Many2one(
        comodel_name='res.partner',
        string='Nombre cliente',
    )
    cobrado = fields.Monetary(
        string='Cobrado'
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Moneda',
    )
    comision = fields.Monetary(
        string='Comisión',
    )
    pagado = fields.Boolean(
        string='Pagado',
    )
    pago_comision_id = fields.Many2one(
        comodel_name='xe.pago.comisiones',
        string='Pago de comisiones',
    )
    sale_order_ids = fields.Many2many(
        comodel_name='sale.order',
        string='Ordenes de venta',
    )
    sale_orders = fields.Char(
        string='Órdenes de venta',
        compute='_compute_sale_orders',
    )
    payment_id = fields.Many2one(
        comodel_name='account.payment',
        string='Cobro',
    )
    por_agente = fields.Float(
        string='Porcentaje agente',
    )

    def action_eliminar(self):
        if self._context.get('params', False):
            model = self._context['params'].get('model', False)
            if model and model == 'xe.pago.comisiones' and id:
                self.pago_comision_id.write({
                    'comisiones_ids': [(3, self.id)]
                })
                self.pago_comision_id = False
