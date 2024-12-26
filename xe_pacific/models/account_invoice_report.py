from odoo import fields, models


class AccountInvoiceReport(models.Model):
    _inherit = 'account.invoice.report'

    pos_store = fields.Many2one('res.partner', readonly=True)

    def _select(self):
        return super()._select() + ", move.pos_store as pos_store"
