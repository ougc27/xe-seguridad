import logging
_logger = logging.getLogger(__name__)

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
