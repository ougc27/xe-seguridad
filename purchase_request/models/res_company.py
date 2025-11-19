# -*- coding: utf-8 -*-
from odoo import models, api, _

class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        ctx = dict(self.env.context or {})
        if ctx.get('show_all_companies'):
            domain = list(args)
            if name:
                domain = ['|', ('id', '=', name)] + domain if name.isdigit() else [('name', operator, name)] + domain
            companies = self.sudo().search(domain, limit=limit)
            return companies.name_get()
        return super(ResCompany, self).name_search(name=name, args=args, operator=operator, limit=limit)
