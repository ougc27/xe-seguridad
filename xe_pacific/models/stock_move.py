from odoo import fields, models, api, _
from odoo.exceptions import UserError


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _default_group_id(self):
        if self.env.context.get('default_picking_id'):
            return self.env['stock.picking'].browse(self.env.context['default_picking_id']).group_id.id
        return False

    group_id = fields.Many2one('procurement.group',
        'Procurement Group',
        default=_default_group_id, 
        index=True,
        tracking=True)

    can_edit_moves_in_transit = fields.Boolean(
        compute='_compute_can_edit_moves_in_transit', store=False
    )

    shipment_kanban_status = fields.Selection(related="picking_id.shipment_task_status")

    picking_scheduled_date = fields.Datetime(related="picking_id.scheduled_date")

    @api.depends('picking_id.state')
    @api.depends_context('uid')
    def _compute_can_edit_moves_in_transit(self):
        user = self.env.user
        for move in self:
            move.can_edit_moves_in_transit = not (
                move.picking_id.state == 'transit'
                and not user.has_group('base.group_system')
            )

    @api.depends('state', 'picking_id.is_locked')
    def _compute_is_initial_demand_editable(self):
        for move in self:
            if move.picking_id.picking_type_code == 'outgoing':
                move.is_initial_demand_editable = False
                continue
            move.is_initial_demand_editable = not move.picking_id.is_locked or move.state == 'draft'

    def write(self, vals):
        for rec in self:
            new_quantity = vals.get("quantity", rec.quantity)
            if new_quantity != rec.quantity:
                if rec.picking_id.state == "transit":
                    raise UserError(_("You cannot change the demanded quantity in transit state"))
                if new_quantity > rec.product_uom_qty:
                    raise UserError(_("You cannot update the quantity with a quantity greater than the demand"))
        return super().write(vals)
