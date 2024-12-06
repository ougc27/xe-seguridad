# coding: utf-8

from odoo import api, fields, models
from odoo.exceptions import UserError


class XePagoComisiones(models.Model):
    _name = 'xe.pago.comisiones'
    _description = 'Pago de comisiones'

    @api.depends('comisiones_ids')
    def _compute_total(self):
        for record in self:
            currency_id = False
            record.total = sum(record.comisiones_ids.mapped('comision'))
            if record.comisiones_ids:
                currency_id = record.comisiones_ids[0].currency_id
            record.currency_id = currency_id

    name = fields.Char(
        string='Número'
    )
    agente_id = fields.Many2one(
        comodel_name='xe.agente',
        string='Agente',
    )
    fecha_inicial = fields.Date(
        string='Fecha inicial',
    )
    fecha_final = fields.Date(
        string='Fecha final',
    )
    fecha_pago = fields.Date(
        string='Fecha de pago',
    )
    comisiones_ids = fields.Many2many(
        comodel_name='xe.comisiones'
    )
    total = fields.Monetary(
        string='Total',
        compute='_compute_total',
        store=True,
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Moneda',
        compute='_compute_total',
        store=True,
    )
    state = fields.Selection(
        selection=[
            ('borrador', 'Borrador'),
            ('confirmado', 'Confirmado'),
            ('pagado', 'Pagado'),
        ],
        string='Estado',
        default='borrador',
    )

    @api.model_create_multi
    def create(self, vals_list):
        pago_comisiones_ids = super(XePagoComisiones, self).create(vals_list)
        for pago_comisiones_id in pago_comisiones_ids:
            pago_comisiones_id.name = self.env['ir.sequence'].next_by_code('xe.pago.comision')
        return pago_comisiones_ids

    def action_confirmar(self):
        for record in self:
            if not record.comisiones_ids:
                raise UserError('Debe listar al menos 1 comisión a pagar.')
            if record.state == 'borrador':
                record.state = 'confirmado'

    def action_calcular(self):
        comisiones_ids = self.env['xe.comisiones'].search([
            ('agente_id', '=', self.agente_id.id),
            ('fecha_cobro', '>=', self.fecha_inicial),
            ('fecha_cobro', '<=', self.fecha_final),
            ('pagado', '=', False),
            ('pago_comision_id', 'in', [self.id, False])
        ])
        self.comisiones_ids = [(6, 0, comisiones_ids.ids)]
        comisiones_ids.write({
            'pago_comision_id': self.id,
        })

    def action_pagar(self, fecha_pago=None):
        if not fecha_pago:
            fecha_pago = fields.Date.context_today(self)
        for record in self:
            if record.state == 'confirmado':
                record.comisiones_ids.write({
                    'pagado': True,
                })
                record.state = 'pagado'
                record.fecha_pago = fecha_pago

    def action_romper_pago(self):
        for record in self:
            if record.state == 'pagado':
                record.comisiones_ids.write({
                    'pagado': False,
                })
                record.state = 'confirmado'
                record.fecha_pago = False

    def unlink(self):
        for record in self:
            if record.state != 'borrador':
                raise UserError('Sólo se pueden eliminar pagos de comisiones en estado borrador.')
        return super(XePagoComisiones, self).unlink()

    def action_regresar_borrador(self):
        for record in self:
            if record.state == 'confirmado':
                record.state = 'borrador'
