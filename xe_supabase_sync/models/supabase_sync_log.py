# -*- coding: utf-8 -*-
from odoo import fields, models


class SupabaseSyncLog(models.Model):
    _name = 'supabase.sync.log'
    _description = 'Supabase sync run history'
    _order = 'started_at desc'

    job_id = fields.Many2one(
        'supabase.sync.job', string='Job', required=True, ondelete='cascade', index=True)
    started_at = fields.Datetime(string='Started At', required=True)
    finished_at = fields.Datetime(string='Finished At')
    status = fields.Selection([
        ('running', 'Running'),
        ('success', 'OK'),
        ('error', 'Error'),
    ], string='Status', required=True, default='running')
    rows_synced = fields.Integer(string='Rows Synced', default=0)
    error_message = fields.Text(string='Error Message')
