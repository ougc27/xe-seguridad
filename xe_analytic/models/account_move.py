from odoo import fields, models, api


class AccountMove(models.Model):
    _inherit = 'account.move'

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

    @api.depends('line_ids', 'x_order_id')
    def _compute_warehouse_id(self):
        for record in self:
            order_id = record.x_order_id
            if order_id:
                record['x_studio_almacen_id'] = order_id.warehouse_id.id
            if record.purchase_order_count >= 1:
                record['x_studio_almacen_id'] = record.line_ids[0].purchase_order_id.picking_type_id.warehouse_id.id
