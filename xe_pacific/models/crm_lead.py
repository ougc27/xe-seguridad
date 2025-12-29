# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models


class Lead(models.Model):
    _inherit = 'crm.lead'

    vambe_brands = fields.Selection([
        ('gott', 'XE Brands'),
        ('protecto_home', 'Romax Comercial SA de CV'),
    ], string='Vambe Brands')
