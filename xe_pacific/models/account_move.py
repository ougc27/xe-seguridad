from odoo import models, fields, api


class AccountMove(models.Model):
    _inherit = 'account.move'

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
    )

    x_order_id = fields.Many2one('sale.order',
        related='invoice_line_ids.x_order_id',
        store=True,
        readonly=True
    )

    x_studio_almacen_id = fields.Many2one(
        'stock.warehouse',
        'Warehouse',
        compute='_compute_warehouse_id',
        readonly=True,
        store=True,
        tracking=True
    )

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        'Warehouse',
    )

    @api.depends('line_ids', 'x_order_id', 'warehouse_id')
    def _compute_warehouse_id(self):
        for record in self:
            if record.warehouse_id:
                record['x_studio_almacen_id'] = record.warehouse_id
                continue
            order_id = record.x_order_id
            if order_id:
                record['x_studio_almacen_id'] = order_id.warehouse_id.id
            if record.purchase_order_count >= 1:
                record['x_studio_almacen_id'] = record.line_ids[0].purchase_order_id.picking_type_id.warehouse_id.id
