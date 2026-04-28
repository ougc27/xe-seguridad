# -*- coding: utf-8 -*-
# Copyright 2015-2016 Akretion (http://www.akretion.com)
# @author Alexis de Lattre <alexis.delattre@akretion.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ProductCategory(models.Model):
    _inherit = "product.category"

    allow_negative_stock = fields.Boolean(
        help="Allow negative stock levels for the stockable products "
        "attached to this category. The options doesn't apply to products "
        "attached to sub-categories of this category.",
    )

    def _check_negative_setting(self, vals):
        if vals.get('allow_negative_stock'):
            raise UserError(_(
                "Operation not allowed: Enabling negative stock is prohibited "
                "by company inventory policies."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_negative_setting(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_negative_setting(vals)
        return super().write(vals)



class ProductTemplate(models.Model):
    _inherit = "product.template"

    allow_negative_stock = fields.Boolean(
        help="If this option is not active on this product nor on its "
        "product category and that this product is a stockable product, "
        "then the validation of the related stock moves will be blocked if "
        "the stock level becomes negative with the stock move.",
    )

    def _check_negative_setting(self, vals):
        if vals.get('allow_negative_stock'):
            raise UserError(_(
                "Operation not allowed: Enabling negative stock is prohibited "
                "by company inventory policies."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_negative_setting(vals)
        return super().create(vals_list)

    def write(self, vals):
        self._check_negative_setting(vals)
        return super().write(vals)

