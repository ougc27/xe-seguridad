from odoo import _, api, fields, models
from odoo.exceptions import UserError


class MeliQuarantineReturnWizard(models.TransientModel):
    _name = 'meli.quarantine.return.wizard'
    _description = (
        "Mercado Libre Quarantine Return Wizard (2026-09-24 user "
        "request, Phase 2 of the Monterrey XE2 total-cancellation "
        "project): confirms how much of a cancelled non-Full order's "
        "stock — sitting in the shared 'Devoluciones en tránsito ML' "
        "location since Phase 1 automatically moved it there — has "
        "actually, physically come back to the warehouse. Pre-filled "
        "with only what's still left to confirm (never the order's "
        "original full quantity again once a previous partial run "
        "already moved some of it)."
    )

    sale_order_id = fields.Many2one(
        'sale.order', string='Sale Order', required=True, readonly=True,
    )
    company_id = fields.Many2one(related='sale_order_id.company_id')
    meli_stock_return_state = fields.Selection(
        related='sale_order_id.meli_stock_return_state',
        string='Physical Return Status',
        help="Plain (non-dotted) related field so the 'Settle' button's "
             "own invisible condition can reference it directly — "
             "Odoo's view validator requires every field a modifier "
             "expression touches to be present in the view by name.",
    )
    location_id = fields.Many2one(
        'stock.location', string='Quarantine Location', required=True,
        domain="[('company_id', '=', company_id), ('name', '=', 'Cuarentena')]",
        help="Open to every quarantine location in the company, not just "
             "this order's own warehouse — defaults to the order's own "
             "warehouse quarantine, but a different one can be chosen "
             "(2026-09-24 user decision).",
    )
    line_ids = fields.One2many(
        'meli.quarantine.return.wizard.line', 'wizard_id', string='Products',
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        order_id = vals.get('sale_order_id')
        if not order_id:
            return vals
        order = self.env['sale.order'].browse(order_id)
        remaining = order._meli_quarantine_remaining_by_product()
        if 'line_ids' in fields_list:
            vals['line_ids'] = [
                (0, 0, {
                    'product_id': product.id,
                    'quantity': qty,
                    'max_quantity': qty,
                })
                for product, qty in remaining.items()
            ]
        if 'location_id' in fields_list and order.warehouse_id:
            default_location = self.env['stock.location'].sudo().search([
                ('company_id', '=', order.company_id.id),
                ('name', '=', 'Cuarentena'),
                ('id', 'child_of', order.warehouse_id.view_location_id.id),
            ], limit=1)
            if default_location:
                vals['location_id'] = default_location.id
        return vals

    def action_confirm(self):
        self.ensure_one()
        quantities = {
            line.product_id: line.quantity
            for line in self.line_ids if line.quantity > 0
        }
        if not quantities:
            raise UserError(_(
                "Enter at least one quantity greater than zero."
            ))
        if not self.location_id:
            raise UserError(_("Select a quarantine location first."))
        fully_done = self.sale_order_id._meli_quarantine_move_stock(
            quantities, self.location_id.id,
        )
        self.sale_order_id.meli_stock_return_state = (
            'done' if fully_done else 'partial'
        )
        return {'type': 'ir.actions.act_window_close'}

    def action_settle(self):
        self.ensure_one()
        if self.sale_order_id.meli_stock_return_state != 'partial':
            raise UserError(_(
                "Settling is only available once this order's own "
                "return is already Partial."
            ))
        self.sale_order_id.meli_stock_return_state = 'settled'
        self.sale_order_id.message_post(body=_(
            "Marked as Settled — the remaining stock for this order will "
            "not be returned."
        ))
        return {'type': 'ir.actions.act_window_close'}


class MeliQuarantineReturnWizardLine(models.TransientModel):
    _name = 'meli.quarantine.return.wizard.line'
    _description = 'Mercado Libre Quarantine Return Wizard Line'

    wizard_id = fields.Many2one(
        'meli.quarantine.return.wizard', required=True, ondelete='cascade',
    )
    product_id = fields.Many2one('product.product', required=True, readonly=True)
    max_quantity = fields.Float(
        string='Remaining', readonly=True,
        help="Still sitting in transit for this order, not yet confirmed "
             "into quarantine — the quantity field below can only be "
             "lowered, never raised past this.",
    )
    quantity = fields.Float(string='Quantity to Confirm')
