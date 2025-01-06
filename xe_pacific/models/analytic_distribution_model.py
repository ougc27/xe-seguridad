# -*- coding: utf-8 -*-

from odoo import fields, models, api
from collections import defaultdict


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

    @api.model
    def _get_distribution(self, vals):
        """ Returns a combined analytic distribution for all matching models.
            The distributions are summed when multiple records match the given vals.
        """
        domain = []
        for fname, value in vals.items():
            domain += self._create_domain(fname, value) or []
        
        matching_records = self.search(domain)
        
        combined_distribution = defaultdict(float)
        fnames = set(self._get_fields_to_check())
        
        for rec in matching_records:
            try:
                score = sum(rec._check_score(key, vals.get(key)) for key in fnames)
                if score > 0:
                    for key, value in rec.analytic_distribution.items():
                        combined_distribution[key] += value
            except:
                continue
        
        return dict(combined_distribution)
