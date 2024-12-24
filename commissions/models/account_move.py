# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

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
        string='Is commission admin',
        compute='_compute_is_commission_admin',
        default=_default_is_commission_admin,
    )
    commission_ids = fields.Many2many(
        comodel_name='xe.commissions',
        string='Commissions',
        copy=False,
    )
    commission_qty = fields.Integer(
        string='Commissions quantity',
        compute='_compute_commission_qty',
    )

    def action_open_commissions(self):
        action = self.env['ir.actions.actions']._for_xml_id('commissions.commissions_open_action')
        action['domain'] = [('id', 'in', self.commission_ids.ids)]
        return action

    def button_draft(self):
        paid_commission_ids = self.commission_ids.filtered(
            lambda o: o.paid
        )
        if paid_commission_ids:
            raise UserError(_('You cannot reset this invoice to draft because it has paid commissions.'))
        return super(AccountMove, self).button_draft()

    def button_cancel(self):
        paid_commission_ids = self.commission_ids.filtered(
            lambda o: o.paid
        )
        if paid_commission_ids:
            raise UserError(_('You cannot cancel this invoice because it has paid commissions.'))
        return super(AccountMove, self).button_cancel()

    def button_request_cancel(self):
        paid_commission_ids = self.commission_ids.filtered(
            lambda o: o.paid
        )
        if paid_commission_ids:
            raise UserError(_('You cannot cancel this invoice because it has paid commissions.'))
        return super(AccountMove, self).button_cancel()

    @api.onchange('partner_id')
    def onchange_partner_id(self):
        agent1_id = False
        agent1_per = 0
        agent2_id = False
        agent2_per = 0
        if self.partner_id:
            agent1_id = self.partner_id.agent1_id
            agent1_per = self.partner_id.agent1_per
            agent2_id = self.partner_id.agent2_id
            agent2_per = self.partner_id.agent2_per
        self.agent1_id = agent1_id
        self.agent1_per = agent1_per
        self.agent2_id = agent2_id
        self.agent2_per = agent2_per

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        move_id = super(AccountMove, self).copy(default)
        move_id.onchange_partner_id()
        return move_id

    def js_assign_outstanding_line(self, line_id):
        res = super(AccountMove, self).js_assign_outstanding_line(line_id)
        payment_id = self.env['account.move.line'].browse(line_id).payment_id
        for line_id in payment_id.line_ids:
            for matched_debit_id in line_id.matched_debit_ids:
                move_id = matched_debit_id.debit_move_id.move_id
                collected = matched_debit_id.amount / 1.16
                if move_id.agent1_id and move_id.agent1_per:
                    agent = move_id.agent1_id.id
                    commission = collected * move_id.agent1_per / 100
                    self.create_commission(agent, move_id, collected, commission, payment_id.id, 1, move_id.agent1_per)
                if move_id.agent2_id and move_id.agent2_per:
                    agent = move_id.agent2_id.id
                    commission = collected * move_id.agent2_per / 100
                    self.create_commission(agent, move_id, collected, commission, payment_id.id, 2, move_id.agent2_per)
        return res

    def create_commission(self, agent, move_id, collected, commission, id_payment, position, agent_per):
        payment_id = self.env['account.payment'].browse(id_payment)
        commission_id = self.env['xe.commissions'].create({
            'position': position,
            'agent_id': agent,
            'date': move_id.invoice_date,
            'move_id': move_id.id,
            'collected_date': payment_id.date,
            'customer_id': move_id.partner_id.id,
            'collected': collected,
            'currency_id': payment_id.currency_id.id,
            'commission': commission,
            'paid': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'agent_per': agent_per,
        })
        move_id.write({
            'commission_ids': [(4, commission_id.id)]
        })