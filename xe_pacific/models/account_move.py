from odoo import models, fields


class AccountMove(models.Model):
    _inherit = 'account.move'

    x_order_id = fields.Many2one('sale.order',
        related='invoice_line_ids.x_order_id',
        store=True,
        readonly=True
    )

    pos_store = fields.Many2one('res.partner', 
        related='x_order_id.pos_store',
        readonly=True,
        store=True
    )
