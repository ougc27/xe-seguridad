# coding: utf-8

from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _convert_to_tax_base_line_dict_xebrands(self, qty_pending):
        self.ensure_one()
        return self.env['account.tax']._convert_to_tax_base_line_dict(
            self,
            partner=self.order_id.partner_id,
            currency=self.order_id.currency_id,
            product=self.product_id,
            taxes=self.tax_id,
            price_unit=self.price_unit,
            quantity=qty_pending,
            discount=self.discount,
            price_subtotal=self.price_subtotal,
        )