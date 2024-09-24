from odoo import fields, models


class SaleReport(models.Model):
    
    _inherit = 'sale.report'

    margin = fields.Float("Margin", groups="account.group_account_manager")
