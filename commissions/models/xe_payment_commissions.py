# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class XePaymentCommissions(models.Model):
    _name = 'xe.payment.commissions'
    _description = 'Payment commissions'

    @api.depends('commissions_ids')
    def _compute_total(self):
        for record in self:
            currency_id = False
            record.total = sum(record.commissions_ids.mapped('commission'))
            if record.commissions_ids:
                currency_id = record.commissions_ids[0].currency_id
            record.currency_id = currency_id

    name = fields.Char(
        string='Number'
    )
    agent_id = fields.Many2one(
        comodel_name='xe.agent',
        string='Agent',
    )
    start_date = fields.Date(
        string='Start date',
    )
    end_date = fields.Date(
        string='End date',
    )
    payment_date = fields.Date(
        string='Payment date',
    )
    commissions_ids = fields.Many2many(
        comodel_name='xe.commissions'
    )
    total = fields.Monetary(
        string='Total',
        compute='_compute_total',
        store=True,
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        compute='_compute_total',
        store=True,
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('paid', 'Paid'),
        ],
        string='State',
        default='draft',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        default=lambda self: self.env.company,
    )

    @api.model_create_multi
    def create(self, vals_list):
        payment_commissions_ids = super(XePaymentCommissions, self).create(vals_list)
        for payment_commission_id in payment_commissions_ids:
            sequence_id = self.env['ir.sequence'].search([
                ('code', '=', 'xe.payment.commission'),
                ('company_id', '=', payment_commission_id.company_id.id),
            ])
            if not sequence_id:
                raise UserError(_('Sequence not found'))
            payment_commission_id.name = sequence_id.next_by_id()
        return payment_commissions_ids

    def action_confirm(self):
        for record in self:
            if not record.commissions_ids:
                raise UserError(_('You must list at least one commission to pay.'))
            if record.state == 'draft':
                record.state = 'confirmed'

    def action_calculate(self):
        commissions_ids = self.env['xe.commissions'].search([
            ('agent_id', '=', self.agent_id.id),
            ('collected_date', '>=', self.start_date),
            ('collected_date', '<=', self.end_date),
            ('paid', '=', False),
            ('payment_commission_id', 'in', [self.id, False])
        ])
        self.commissions_ids = [(6, 0, commissions_ids.ids)]
        commissions_ids.write({
            'payment_commission_id': self.id,
        })

    def action_pay(self, payment_date=None):
        if not payment_date:
            payment_date = fields.Date.context_today(self)
        for record in self:
            if record.state == 'confirmed':
                record.commissions_ids.write({
                    'paid': True,
                })
                record.state = 'paid'
                record.payment_date = payment_date

    def action_separate_payment(self):
        for record in self:
            if record.state == 'paid':
                record.commissions_ids.write({
                    'paid': False,
                })
                record.state = 'confirmed'
                record.payment_date = False

    def unlink(self):
        for record in self:
            if record.state != 'draft':
                raise UserError(_('Only commission payments in draft status can be deleted.'))
        return super(XePaymentCommissions, self).unlink()

    def action_return_draft(self):
        for record in self:
            if record.state == 'confirmed':
                record.state = 'draft'
