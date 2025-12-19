# -*- coding: utf-8 -*-

from odoo import models, fields, api


class WhatsappResponseMetric(models.Model):
    _name = 'whatsapp.response.metric'
    _description = 'WhatsApp First Response Metric'
    _order = 'first_agent_message_at desc'

    user_id = fields.Many2one(
        'res.users',
        string='Agent',
        required=True,
        index=True,
    )

    whatsapp_number = fields.Char(
        string='WhatsApp Number',
        required=True,
        index=True,
    )

    wa_account_id = fields.Many2one(
        'whatsapp.account',
        string='WhatsApp Business Account',
        required=True,
        index=True,
    )

    first_customer_message_at = fields.Datetime(
        string='First Customer Message At',
        required=True,
    )

    first_agent_message_at = fields.Datetime(
        string='First Agent Message At',
        required=True,
    )

    response_time_minutes = fields.Float(
        string='Response Time (Minutes)',
        compute='_compute_response_time',
        store=True,
    )

    @api.depends('first_customer_message_at', 'first_agent_message_at')
    def _compute_response_time(self):
        for rec in self:
            if rec.first_customer_message_at and rec.first_agent_message_at:
                delta = rec.first_agent_message_at - rec.first_customer_message_at
                rec.response_time_minutes = delta.total_seconds() / 60.0
            else:
                rec.response_time_minutes = 0.0
