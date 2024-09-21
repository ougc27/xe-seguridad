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
