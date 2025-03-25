# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import models, fields, api


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    client_barcode = fields.Char(
        string='Client Product Barcode',
        compute='_compute_client_barcode',
        store=True)

    @api.depends('partner_id', 'product_id', 'sale_line_ids')
    def _compute_client_barcode(self):
        for line in self:
            if not line.product_id or not line.sale_line_ids:
                line.client_barcode = False
                return
            order = line.sale_line_ids[0].order_id
            client = line.product_id._select_client(
                partner_id=line.move_id.partner_id,
                quantity=line.quantity,
                date=order.date_order and order.date_order.date(),
                uom_id=line.product_uom_id)
            if client:
                line.client_barcode = client.product_barcode
            else:
                line.client_barcode = False

    def _check_reconciliation(self):
        for line in self:
            if line.quantity != 0:
                super(AccountMoveLine, line)._check_reconciliation()

    def _check_tax_lock_date(self):
        for line in self:
            if line.quantity != 0:
                super(AccountMoveLine, line)._check_tax_lock_date()

    @api.onchange('price_unit')
    def _onchange_price_unit(self):
        for move in self:
            if move.is_downpayment:
                # Update related sale order line
                move.sale_line_ids.write({
                    'price_unit': move.price_unit,
                })

                # Don't allow to set a price unit lower than the reconciled amounts
                downpayments = self.env['sale.down.payment'].search([
                    ('invoice_id', '=', move._origin.move_id.id),
                ])
                reconciled_amount = sum(downpayments.order_line_id.mapped('price_unit'))
                if reconciled_amount > move.price_unit:
                    raise UserError(_("Down Payment amount cannot be lower than the reconciled amounts."))