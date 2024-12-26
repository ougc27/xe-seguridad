# coding: utf-8

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class XeAgent(models.Model):
    _name = 'xe.agent'
    _description = 'Agent'

    name = fields.Many2one(
        comodel_name='hr.employee',
        string='Agent',
    )
    number = fields.Integer(
        string='Agent number',
    )
    commission = fields.Float(
        string='Commission (%)',
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        default=lambda self: self.env.company,
    )

    def validate_data(self):
        if self.number <= 0:
            raise UserError(_('The agent number must be greater than 0.'))
        if self.commission <= 0:
            raise UserError(_("The agent's commission must be greater than 0."))
        duplicate_agent_ids = self.search([
            ('name', '=', self.name.id),
            ('id', '!=', self.id),
        ])
        if duplicate_agent_ids:
            raise UserError(_('A record already exists for agent {0}.'.format(self.name.name)))

    @api.model_create_multi
    def create(self, val_list):
        agent_ids = super(XeAgent, self).create(val_list)
        for agent_id in agent_ids:
            agent_id.validate_data()
        return agent_ids

    def write(self, vals):
        res = super(XeAgent, self).write(vals)
        for agent_id in self:
            agent_id.validate_data()
        return res
