# -*- coding: utf-8 -*-

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    not_calculate_for_billing = fields.Boolean(
        string='Not calculate for billing'
    )
