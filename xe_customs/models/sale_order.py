# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    client_barcode = fields.Char(
        string='Client Product Barcode',
        compute='_compute_client_barcode',
        store=True)

    @api.depends('order_id.partner_id', 'product_id')
    def _compute_client_barcode(self):
        for line in self:
            if not line.product_id or not line.order_id.partner_id:
                line.client_barcode = False
                return
            client = line.product_id._select_client(
                partner_id=line.order_id.partner_id,
                quantity=line.product_uom_qty,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom)
            if client:
                line.client_barcode = client.product_barcode
            else:
                line.client_barcode = False

    