# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class Lead(models.Model):
    _inherit = 'crm.lead'

    vambe_brands = fields.Selection([
        ('gott', 'XE Brands'),
        ('protecto_home', 'Romax Comercial SA de CV'),
    ], string='Vambe Brands')

    def write(self, vals):
        if 'user_id' in vals and vals.get('user_id') is False:
            for rec in self:
                if rec.vambe_brands:
                    self.env['ir.logging'].sudo().create({
                        'name': 'Vambe Lead user_id set to NULL',
                        'type': 'server',
                        'dbname': self.env.cr.dbname,
                        'level': 'WARNING',
                        'message': f'user_id wiped. vals={vals}',
                        'path': 'crm.lead',
                        'func': 'write',
                        'line': '15',
                    })
        return super().write(vals)
