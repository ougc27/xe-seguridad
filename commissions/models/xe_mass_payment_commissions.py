# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class XeMassPaymentCommissions(models.Model):
    _name = 'xe.mass.payment.commissions'
    _description = 'Mass payment commissions'

    @api.depends('payment_commissions_ids')
    def _compute_payment_commissions_qty(self):
        for record in self:
            record.payment_commissions_qty = len(record.payment_commissions_ids)

    @api.depends('payment_commissions_ids')
    def _compute_total(self):
        for record in self:
            currency_id = False
            total = 0
            if record.payment_commissions_ids:
                currency_id = record.payment_commissions_ids[0].currency_id
                total = sum(record.payment_commissions_ids.mapped('total'))
            record.currency_id = currency_id
            record.total = total

    name = fields.Char(
        string='Number',
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
    agent_ids = fields.Many2many(
        comodel_name='xe.agent',
        string='Agents',
    )
    total = fields.Float(
        string='Total',
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
    payment_commissions_ids = fields.Many2many(
        comodel_name='xe.payment.commissions',
        string='Commissions',
    )
    payment_commissions_qty = fields.Integer(
        string='Payment commissions quantity',
        compute='_compute_payment_commissions_qty'
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        compute='_compute_total',
        store=True,
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        default=lambda self: self.env.company,
    )

    def action_calculate(self):
        for agent_id in self.agent_ids:
            commissions_ids = self.env['xe.commissions'].search([
                ('agent_id', '=', agent_id.id),
                ('collected_date', '>=', self.start_date),
                ('collected_date', '<=', self.end_date),
                ('paid', '=', False),
                ('payment_commission_id', '=', False)
            ])
            if commissions_ids:
                payment_commissions_id = self.env['xe.payment.commissions'].create({
                    'agent_id': agent_id.id,
                    'start_date': self.start_date,
                    'end_date': self.end_date,
                    'commissions_ids': [(6, 0, commissions_ids.ids)],
                })
                commissions_ids.write({
                    'payment_commission_id': payment_commissions_id.id,
                })
                self.payment_commissions_ids = [(4, payment_commissions_id.id)]

    def action_confirm(self):
        self.payment_commissions_ids.action_confirm()
        self.state = 'confirmed'

    def action_pay(self):
        payment_date = fields.Date.context_today(self)
        self.payment_commissions_ids.action_pay(payment_date)
        self.state = 'paid'
        self.payment_date = payment_date

    def action_return_draft(self):
        self.payment_commissions_ids.action_return_draft()
        self.state = 'draft'

    def action_separate_payment(self):
        self.payment_commissions_ids.action_separate_payment()
        self.state = 'confirmed'
        self.payment_date = False

    def action_open_payment_commissions(self):
        action = self.env['ir.actions.actions']._for_xml_id('commissions.payment_commissions_open_action')
        action['domain'] = [('id', 'in', self.payment_commissions_ids.ids)]
        return action

    @api.model_create_multi
    def create(self, vals_list):
        mass_payment_commissions_ids = super(XeMassPaymentCommissions, self).create(vals_list)
        for mass_payment_commissions_id in mass_payment_commissions_ids:
            sequence_id = self.env['ir.sequence'].search([
                ('code', '=', 'xe.mass.payment.commission'),
                ('company_id', '=', mass_payment_commissions_id.company_id.id),
            ])
            if not sequence_id:
                raise UserError(_('Sequence not found'))
            mass_payment_commissions_id.name = sequence_id.next_by_id()
        return mass_payment_commissions_ids
