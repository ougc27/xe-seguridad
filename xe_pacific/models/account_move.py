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

    order_ids = fields.Many2many(
        'sale.order',
        'account_move_sale_order_rel',
        'move_id',
        'order_id',
        string='Sale Orders',
        compute='_compute_order_ids'
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

    @api.depends('invoice_origin')
    def _compute_order_ids(self):
        for record in self:
            if record.invoice_origin:
                order_names = [name.strip() for name in record.invoice_origin.split(',')]
                record.order_ids = self.env['sale.order'].search([('name', 'in', order_names)]).ids
            record.order_ids = False
