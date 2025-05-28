# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.depends('commission_ids')
    def _compute_commission_qty(self):
        for record in self:
            record.commission_qty = len(record.commission_ids)

    commission_ids = fields.Many2many(
        comodel_name='xe.commissions',
        string='Commissions',
        copy=False,
    )
    commission_qty = fields.Integer(
        string='Commissions quantity',
        compute='_compute_commission_qty',
    )
    pos_originated = fields.Boolean(
        string='Pos originated',
    )
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

    exclude_recalculation = fields.Boolean(
        string='Exclude recalculation',
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
        return super(AccountMove, self).button_request_cancel()

    def js_assign_outstanding_line(self, line_id):
        res = super(AccountMove, self).js_assign_outstanding_line(line_id)
        payment_id = self.env['account.move.line'].browse(line_id).payment_id
        for line_id in payment_id.line_ids:
            for matched_debit_id in line_id.matched_debit_ids:
                move_id = matched_debit_id.debit_move_id.move_id
                for commission_id in payment_id.commission_ids:
                    move_id.write({'commission_ids': [(4, commission_id.id)]})
                    commission_id.write({'move_ids': [(4, move_id.id)]})
                    commission_id.get_sales()
        return res

    def js_remove_outstanding_partial(self, partial_id):
        partial = self.env['account.partial.reconcile'].browse(partial_id)
        move_id = partial.debit_move_id.move_id
        for commission_id in self.payment_id.commission_ids:
            move_id.write({'commission_ids': [(3, commission_id.id)]})
            commission_id.write({'move_ids': [(3, move_id.id)]})
            commission_id.get_sales()
        return super(AccountMove, self).js_remove_outstanding_partial(partial_id)

    def action_exclude_recalculation(self):
        self.filtered(
            lambda o: o.pos_originated
        ).write({
            'exclude_recalculation': True,
        })
