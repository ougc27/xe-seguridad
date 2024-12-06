# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields


class AccountInvoiceReport(models.Model):
    _inherit = "account.invoice.report"

    price_margin = fields.Float(
        string='Margin',
        readonly=True,
        groups="account.group_account_manager")
    inventory_value = fields.Float(
        string='Inventory Value',
        readonly=True,
        groups="account.group_account_manager")
