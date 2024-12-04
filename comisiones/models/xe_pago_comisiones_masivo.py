# coding: utf-8

from odoo import api, fields, models
from odoo.exceptions import UserError


class XePagoComisionesMasivo(models.Model):
    _name = 'xe.pago.comisiones.masivo'
    _description = 'Pago de comisiones masivo'

    @api.depends('pago_comisiones_ids')
    def _compute_ctd_pago_comisiones(self):
        for record in self:
            record.ctd_pago_comisiones = len(record.pago_comisiones_ids)

    @api.depends('pago_comisiones_ids')
    def _compute_total(self):
        for record in self:
            currency_id = False
            total = 0
            if record.pago_comisiones_ids:
                currency_id = record.pago_comisiones_ids[0].currency_id
                total = sum(record.pago_comisiones_ids.mapped('total'))
            record.currency_id = currency_id
            record.total = total

    name = fields.Char(
        string='Número',
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
    agente_ids = fields.Many2many(
        comodel_name='xe.agente',
        string='Agentes',
    )
    total = fields.Float(
        string='Total',
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
    pago_comisiones_ids = fields.Many2many(
        comodel_name='xe.pago.comisiones',
        string='Comisiones',
    )
    ctd_pago_comisiones = fields.Integer(
        string='Cantidad pago comisiones',
        compute='_compute_ctd_pago_comisiones'
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Moneda',
        compute='_compute_total',
        store=True,
    )

    def action_calcular(self):
        for agente_id in self.agente_ids:
            comisiones_ids = self.env['xe.comisiones'].search([
                ('agente_id', '=', agente_id.id),
                ('fecha_cobro', '>=', self.fecha_inicial),
                ('fecha_cobro', '<=', self.fecha_final),
                ('pagado', '=', False),
                ('pago_comision_id', '=', False)
            ])
            if comisiones_ids:
                pago_comisiones_id = self.env['xe.pago.comisiones'].create({
                    'agente_id': agente_id.id,
                    'fecha_inicial': self.fecha_inicial,
                    'fecha_final': self.fecha_final,
                    'comisiones_ids': [(6, 0, comisiones_ids.ids)],
                })
                comisiones_ids.write({
                    'pago_comision_id': pago_comisiones_id.id,
                })
                self.pago_comisiones_ids = [(4, pago_comisiones_id.id)]

    def action_confirmar(self):
        self.pago_comisiones_ids.action_confirmar()
        self.state = 'confirmado'

    def action_pagar(self):
        fecha_pago = fields.Date.context_today(self)
        self.pago_comisiones_ids.action_pagar(fecha_pago)
        self.state = 'pagado'
        self.fecha_pago = fecha_pago

    def action_regresar_borrador(self):
        self.pago_comisiones_ids.action_regresar_borrador()
        self.state = 'borrador'

    def action_romper_pago(self):
        self.pago_comisiones_ids.action_romper_pago()
        self.state = 'confirmado'
        self.fecha_pago = False

    def action_open_pago_comisiones(self):
        action = self.env['ir.actions.actions']._for_xml_id('comisiones.pago_comisiones_open_action')
        action['domain'] = [('id', 'in', self.pago_comisiones_ids.ids)]
        return action

    @api.model_create_multi
    def create(self, vals_list):
        pago_comisiones_masivo_ids = super(XePagoComisionesMasivo, self).create(vals_list)
        for pago_comisiones_masivo_id in pago_comisiones_masivo_ids:
            pago_comisiones_masivo_id.name = self.env['ir.sequence'].next_by_code('xe.pago.comision.masivo')
        return pago_comisiones_masivo_ids
