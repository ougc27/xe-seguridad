# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    def _compute_is_advanced_commission_user(self):
        self.is_advanced_commission_user = self.env.user.has_group('commissions.advanced_user_commissions_group') and not self.env.user.has_group('commissions.commissions_payment_group')

    def _compute_is_commission_admin(self):
        self.is_commission_admin = self.env.user.has_group('commissions.commission_admin_group')

    def _default_is_advanced_commission_user(self):
        return self.env.user.has_group('commissions.advanced_user_commissions_group') and not self.env.user.has_group('commissions.commissions_payment_group')

    def _default_is_commission_admin(self):
        return self.env.user.has_group('commissions.commission_admin_group')

    @api.depends('commission_ids')
    def _compute_commission_qty(self):
        for record in self:
            record.commission_qty = len(record.commission_ids)

    agent1_id = fields.Many2one(
        comodel_name='xe.agent',
        string='Agent 1',
    )
    agent1_per = fields.Float(
        string='Agent 1 percentage',
        copy=False,
    )
    agent2_id = fields.Many2one(
        comodel_name='xe.agent',
        string='Agent 2',
    )
    agent2_per = fields.Float(
        string='Agent 2 percentage',
        copy=False,
    )
    is_advanced_commission_user = fields.Boolean(
        string='Is advanced commission user',
        compute='_compute_is_advanced_commission_user',
        default=_default_is_advanced_commission_user,
    )
    is_commission_admin = fields.Boolean(
        string='Is commission administrator',
        compute='_compute_is_commission_admin',
        default=_default_is_commission_admin,
    )
    manual_creation = fields.Boolean(
        string='Manual creation'
    )
    commission_ids = fields.Many2many(
        comodel_name='xe.commissions',
        string='Commission',
        copy=False,
    )
    commission_qty = fields.Integer(
        string='Commissions quantity',
        compute='_compute_commission_qty',
    )
    exclude_recalculation = fields.Boolean(
        string='Exclude recalculation',
    )

    def action_exclude_recalculation(self):
        self.write({
            'exclude_recalculation': True,
        })

    def check_paid_commissions(self, error):
        if self:
            commissions_ids = self.env['xe.commissions'].search([
                ('payment_id', '=', self.id),
                ('paid', '=', True),
            ])
            if commissions_ids:
                raise UserError(error)

    def delete_unpaid_commissions(self):
        if self:
            commissions_ids = self.env['xe.commissions'].search([
                ('payment_id', '=', self.id),
                ('paid', '=', False),
            ])
            if commissions_ids:
                commissions_ids.sudo().unlink()

    def action_draft(self):
        if self:
            error = _('You cannot reset this payment to draft because it has paid commissions.')
            self.check_paid_commissions(error)
        res = super(AccountPayment, self).action_draft()
        if self:
            self.delete_unpaid_commissions()
        return res

    def action_cancel(self):
        if self:
            error = _('You cannot cancel this payment because it has paid commissions.')
            self.check_paid_commissions(error)
        res =  super(AccountPayment, self).action_cancel()
        if self:
            self.delete_unpaid_commissions()
        return res

    @api.onchange('partner_id')
    def onchange_partner_id(self):
        if self.partner_id:
            self.agent1_id = self.partner_id.agent1_id
            self.agent1_per = self.partner_id.agent1_per
            self.agent2_id = self.partner_id.agent2_id
            self.agent2_per = self.partner_id.agent2_per

    def action_post(self):
        res = super(AccountPayment, self).action_post()
        for payment_id in self:
            data = []
            if (
                payment_id.payment_type == 'inbound' and
                payment_id.journal_id.generates_commission and
                payment_id.destination_account_id.generates_commission
            ):
                if payment_id.partner_id.is_border_zone_iva:
                    collected = payment_id.amount / 1.08
                else:
                    collected = payment_id.amount / 1.16
                if payment_id.agent1_id and payment_id.agent1_per:
                    commission = collected * payment_id.agent1_per / 100
                    data.append(
                        {
                            'position': 1,
                            'agent_id': payment_id.agent1_id.id if payment_id.agent1_id else False,
                            'collected_date': payment_id.date,
                            'customer_id': payment_id.partner_id.id,
                            'collected': collected,
                            'currency_id': payment_id.currency_id.id,
                            'commission': commission,
                            'paid': False,
                            'payment_id': payment_id.id,
                            'agent_per': payment_id.agent1_per,
                        }
                    )
                if payment_id.agent2_id and payment_id.agent2_per:
                    commission = collected * payment_id.agent2_per / 100
                    data.append(
                        {
                            'position': 2,
                            'agent_id': payment_id.agent2_id.id if payment_id.agent2_id else False,
                            'collected_date': payment_id.date,
                            'customer_id': payment_id.partner_id.id,
                            'collected': collected,
                            'currency_id': payment_id.currency_id.id,
                            'commission': commission,
                            'paid': False,
                            'payment_id': payment_id.id,
                            'agent_per': payment_id.agent2_per,
                        }
                    )
            if data:
                commissions_ids = self.env['xe.commissions'].create(data)
                payment_id.commission_ids = [(6, 0, commissions_ids.ids)]
        return res

    @api.model_create_multi
    def create(self, val_list):
        payment_ids = super(AccountPayment, self).create(val_list)
        for payment_id in payment_ids.filtered(lambda o: not o.manual_creation):
            payment_id.onchange_partner_id()
        return payment_ids

    def action_open_commissions(self):
        action = self.env['ir.actions.actions']._for_xml_id('commissions.commissions_open_action')
        action['domain'] = [('id', 'in', self.commission_ids.ids)]
        return action
