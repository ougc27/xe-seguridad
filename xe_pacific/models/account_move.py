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

    picking_ids = fields.Many2many(
        'stock.picking',
        'account_move_picking_rel',
        'move_id',
        'picking_id',
        string='Remissions',
        #context={'from_helpdesk_ticket': True},
        help='Relationship between referrals and invoice.'
    #)
