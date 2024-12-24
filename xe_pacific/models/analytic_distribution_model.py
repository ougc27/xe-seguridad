# -*- coding: utf-8 -*-

from odoo import fields, models


class AccountAnalyticDistributionModel(models.Model):
    _inherit = 'account.analytic.distribution.model'
 
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Warehouse',
        ondelete='cascade',
        check_company=True,
        help="Select a warehouse for which the analytic distribution will be used (e.g. create new customer invoice or Sales order if we select this warehouse, it will automatically take this as an analytic account)",
    )

    team_id = fields.Many2one(
        'crm.team',
        string='Distribution Channel',
        ondelete='cascade',
        check_company=True,
        help="Select a distribution channel for which the analytic distribution will be used (e.g. create new customer invoice or Sales order if we select this distribution channel, it will automatically take this as an analytic account)",
    )
