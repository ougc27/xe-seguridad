# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import fields, models


class SaleReport(models.Model):
    
    _inherit = 'sale.report'

    margin = fields.Float("Margin", groups="account.group_account_manager")
