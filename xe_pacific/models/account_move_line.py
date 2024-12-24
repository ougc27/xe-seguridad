from odoo import models, fields


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    x_order_id = fields.Many2one('sale.order',
        related='sale_line_ids.order_id',
        store=True,
        readonly=True
    )
