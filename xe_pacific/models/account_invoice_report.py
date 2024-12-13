from odoo import fields, models


class AccountInvoiceReport(models.Model):
    _inherit = 'account.invoice.report'

    pos_store = fields.Many2one('res.partner', 
        related='move_id.pos_store',
        readonly=True,
        store=True
    )
