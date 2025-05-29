# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

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

    @api.onchange('agent1_id')
    def _onchange_agent1_id(self):
        agent1_per = 0
        if self.agent1_id:
            agent1_per = self.agent1_id.commission
        self.agent1_per = agent1_per

    @api.onchange('agent2_id')
    def _onchange_agent2_id(self):
        agent2_per = 0
        if self.agent2_id:
            agent2_per = self.agent2_id.commission
        self.agent2_per = agent2_per

    def duplicated_agent(self):
        if self.agent1_id and self.agent2_id and self.agent1_id == self.agent2_id:
            raise UserError(_('Agent 1 and Agent 2 must be different.'))

    def write(self, vals):
        res = super(ResPartner, self).write(vals)
        for record in self:
            record.duplicated_agent()
        return res

    @api.model_create_multi
    def create(self, val_list):
        partner_ids = super(ResPartner, self).create(val_list)
        for partner_id in partner_ids:
            partner_id.duplicated_agent()
        return partner_ids
