from itertools import groupby
from odoo import fields, models, api


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    discount_price = fields.Float(
        string="Coupon Discount Amount",
        digits='Product Price',
        help="Discount amount applied by the coupon on this line"
    )

    program_id = fields.Many2one(
        'loyalty.program',
        string="Coupon Program",
        related='coupon_id.program_id',
        store=True,
        readonly=True,
        help="Loyalty program associated with the applied coupon"
    )

    config_id = fields.Many2one(
        'pos.config',
        string="POS Configuration",
        related='order_id.config_id',
        store=True,
        readonly=True,
        help="Point of Sale configuration associated with this order line"
    )

    user_id = fields.Many2one(
        'res.users',
        string="Salesperson",
        related='order_id.user_id',
        store=True,
        readonly=True,
        help="Salesperson associated with this POS order line"
    )

    def _export_for_ui(self, orderline):
        return {
            'id': orderline.id,
            'qty': orderline.qty,
            'attribute_value_ids': orderline.attribute_value_ids.filtered(lambda av: av.ptav_active).ids,
            'custom_attribute_value_ids': orderline.custom_attribute_value_ids.read(['id', 'name', 'custom_product_template_attribute_value_id', 'custom_value'], load=False),
            'price_unit': orderline.price_unit,
            'skip_change': orderline.skip_change,
            'uuid': orderline.uuid,
            'price_subtotal': orderline.price_subtotal,
            'price_subtotal_incl': orderline.price_subtotal_incl,
            'product_id': orderline.product_id.id,
            'discount': orderline.discount,
            'tax_ids': [[6, False, orderline.tax_ids.mapped(lambda tax: tax.id)]],
            'pack_lot_ids': [[0, 0, lot] for lot in orderline.pack_lot_ids.export_for_ui()],
            'customer_note': orderline.customer_note,
            'refunded_qty': orderline.refunded_qty,
            'price_extra': orderline.price_extra,
            'full_product_name': orderline.full_product_name,
            'refunded_orderline_id': orderline.refunded_orderline_id.id,
            'combo_parent_id': orderline.combo_parent_id.id,
            'combo_line_ids': orderline.combo_line_ids.mapped('id'),
            'discount_price': orderline.discount_price
        }

    def _launch_stock_rule_from_pos_order_lines(self):
        procurements = []
        for line in self:
            line = line.with_company(line.company_id)
            if not line.product_id.type in ('consu', 'product'):
                continue

            group_id = line._get_procurement_group()
            if not group_id:
                group_id = self.env['procurement.group'].create(line._prepare_procurement_group_vals())
                line.order_id.procurement_group_id = group_id

            values = line._prepare_procurement_values(group_id=group_id)
            product_qty = line.qty
            procurement_uom = line.product_id.uom_id
            procurements.append(self.env['procurement.group'].Procurement(
                line.product_id, product_qty, procurement_uom,
                line.order_id.partner_id.property_stock_customer,
                line.name, line.order_id.name, line.order_id.company_id, values))

        if procurements:
            self.env['procurement.group'].run(procurements)

        orders = self.mapped('order_id')
        for order in orders:
            pickings_to_confirm = order.picking_ids
            if pickings_to_confirm:
                tracked_lines = order.lines.filtered(lambda l: l.product_id.tracking != 'none')
                lines_by_tracked_product = groupby(sorted(tracked_lines, key=lambda l: l.product_id.id), key=lambda l: l.product_id.id)

                pickings_to_confirm.action_confirm()

                for product_id, lines in lines_by_tracked_product:
                    lines = self.env['pos.order.line'].concat(*lines)
                    moves = pickings_to_confirm.move_ids.filtered(lambda m: m.product_id.id == product_id)

                    moves.move_line_ids.unlink()

                    for move in moves:
                        qty_needed = move.product_uom_qty

                        lot_assignments = []

                        quants = self.env['stock.quant'].search([
                            ('product_id', '=', move.product_id.id),
                            ('location_id', '=', move.location_id.id),
                            ('quantity', '>', 0),
                            ('lot_id', '!=', False),
                        ])

                        for quant in quants:
                            if qty_needed <= 0:
                                break

                            transit_moves = self.env['stock.move.line'].search([
                                ('product_id', '=', move.product_id.id),
                                ('lot_id', '=', quant.lot_id.id),
                                ('location_id', '=', move.location_id.id),
                                ('picking_id.state', '=', 'transit'),
                                ('company_id', '=', move.company_id.id),
                            ])

                            reserved_in_transit = sum(transit_moves.mapped('quantity'))

                            qty_in_quant = quant.quantity - reserved_in_transit

                            if qty_in_quant <= 0:
                                continue

                            qty_to_assign = min(qty_in_quant, qty_needed)

                            lot_assignments.append({
                                'lot_id': quant.lot_id.id,
                                'quantity': qty_to_assign
                            })

                            qty_needed -= qty_to_assign

                        for assignment in lot_assignments:
                            self.env['stock.move.line'].create({
                                'picking_id': order.picking_ids[0].id,
                                'move_id': move.id,
                                'product_id': move.product_id.id,
                                'location_id': move.location_id.id,
                                'location_dest_id': move.location_dest_id.id,
                                'product_uom_id': move.product_uom.id,
                                'qty_done': assignment['quantity'],
                                'lot_id': assignment['lot_id'],
                            })

                        move._recompute_state()
        if order.shipping_date:
            for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                has_lot_assigned = any(
                    ml.lot_id and ml.qty_done > 0
                    for move in picking.move_ids
                    for ml in move.move_line_ids
                )
                if has_lot_assigned:
                    picking.action_transit()
        return True
