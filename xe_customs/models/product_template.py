# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models

class ProductTemplate(models.Model):
    _inherit = "product.template"

    client_ids = fields.One2many('product.clientinfo', 'product_tmpl_id', 'Clients', depends_context=('company',), help="Define client pricelists.")
    variant_client_ids = fields.One2many('product.clientinfo', 'product_tmpl_id')
