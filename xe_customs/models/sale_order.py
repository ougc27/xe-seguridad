# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

from odoo import api, fields, models, _

from odoo.exceptions import AccessError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _add_client_to_product(self):
        # Add the partner in the client list of the product if the client is not registered for
        # this product. We limit to 10 the number of clients for a product to avoid the mess that
        # could be caused for some generic products ("Miscellaneous").
        for line in self.order_line:
            # Do not add a contact as a client
            partner = line.order_id.partner_id if not line.order_id.partner_id.parent_id else line.order_id.partner_id.parent_id
            already_client = (partner | line.order_id.partner_id) & line.product_id.client_ids.mapped('name')
            if line.product_id and not already_client and len(line.product_id.client_ids) <= 10:
                # Convert the price in the right currency.
                currency = partner.property_purchase_currency_id or line.env.company.currency_id
                price = line.currency_id._convert(line.price_unit, currency, line.company_id, line.order_id.date_order or fields.Date.today(), round=False)
                # Compute the price for the template's UoM, because the client's UoM is related to that UoM.
                if line.product_id.product_tmpl_id.uom_po_id != line.product_uom:
                    default_uom = line.product_id.product_tmpl_id.uom_po_id
                    price = line.product_uom._compute_price(price, default_uom)

                clientinfo = line._prepare_client_info(partner, line, price, currency)
                # In case the order partner is a contact address, a new clientinfo is created on
                # the parent company. In this case, we keep the product name and code.
                client = line.product_id._select_client(
                    partner_id=line.order_id.partner_id,
                    quantity=line.product_uom_qty,
                    date=line.order_id.date_order and line.order_id.date_order.date(),
                    uom_id=line.product_uom)
                if client:
                    clientinfo['product_name'] = client.product_name
                    clientinfo['product_code'] = client.product_code
                vals = {
                    'client_ids': [(0, 0, clientinfo)],
                }
                try:
                    line.product_id.write(vals)
                except AccessError:  # no write access rights -> just ignore
                    break

    # @api.onchange('order_line')
    # def _compute_amount_to_invoice(self):
    #     super(SaleOrder, self)._compute_amount_to_invoice()
    #     raise Exception(self.invoiced_amount)

    def action_confirm(self):
        for order in self:
            res = super(SaleOrder, order).action_confirm()
            order._add_client_to_product()
            return res

class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    #margin = fields.Monetary("Margin", compute='_compute_margin', store=True, groups="account.group_account_manager")

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

    def _prepare_client_info(self, partner, line, price, currency):
        # Prepare clientinfo data when adding a product
        return {
            'name': partner.id,
            'sequence': max(line.product_id.client_ids.mapped('sequence')) + 1 if line.product_id.client_ids else 1,
            'min_qty': 0.0,
            'price': price,
            'currency_id': currency.id,
            'delay': 0,
        }

    @api.onchange('product_id', 'product_uom_qty')
    def onchange_product_id(self):
        for line in self:
            if not line.product_id:
                return

            client = line.product_id._select_client(
                partner_id=line.order_id.partner_id,
                quantity=line.product_uom_qty,
                date=line.order_id.date_order and line.order_id.date_order.date(),
                uom_id=line.product_uom)
            if client:
                line.price_unit = client.price
                name = ""
                if client.product_code:
                    name += "[%s] " % client.product_code
                elif line.product_id.default_code:
                    name += "[%s] " % line.product_id.default_code
                if client.product_name:
                    name += client.product_name
                else:
                    name += line.product_id.name
                line.name = name
            else:
                line.price_unit = line.product_id.lst_price
                line.name = line.product_id.name
