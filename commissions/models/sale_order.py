# coding: utf-8

from odoo import api, fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

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

    def _prepare_invoice(self):
        res = super(SaleOrder, self)._prepare_invoice()
        res['agent1_id'] = self.agent1_id.id
        res['agent1_per'] = self.agent1_per
        res['agent2_id'] = self.agent2_id.id
        res['agent2_per'] = self.agent2_per
        return res

    @api.returns('self', lambda value: value.id)
    def copy(self, default=None):
        sale_order_id = super(SaleOrder, self).copy(default)
        sale_order_id.onchange_partner_id()
        return sale_order_id
