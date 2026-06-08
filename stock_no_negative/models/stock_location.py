# -*- coding: utf-8 -*-
# Copyright 2018 ForgeFlow (https://www.forgeflow.com)
# @author Jordi Ballester <jordi.ballester@forgeflow.com.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class StockLocation(models.Model):
    _inherit = "stock.location"

    allow_negative_stock = fields.Boolean(
        help="Allow negative stock levels for the stockable products "
        "attached to this location.",
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
