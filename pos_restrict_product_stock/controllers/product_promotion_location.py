from odoo import http, fields
from odoo.http import request


class PosPromotionController(http.Controller):

    @http.route('/pos/promotion_data', type='json', auth='user')
    def get_promotion_data(self, company_id, picking_type_id, product_ids, original_pricelist_id, currency_id, tax_id):
        """
        Returns active promotions filtered by:
        - Company
        - Product (required)
        - Warehouse (if the promotion has warehouses defined)
        - Date range

        Returns the tax-excluded price calculated from the stored tax-included price.
        """

        picking_type = request.env['stock.picking.type'].sudo().browse(picking_type_id)
        warehouse_id = picking_type.warehouse_id.id

        if not product_ids:
            return []
        tax = request.env['account.tax'].sudo().browse(tax_id)
        tax_rate = tax.amount or 0.0
        # When the POS tax is tax-included (native price_include or our custom
        # pos_price_include), the POS computes the tax BACKWARDS from the unit
        # price, so the promotion price must be fed WITH tax included as-is
        # (the exact amount captured in the promotion). Only when the tax is
        # tax-excluded do we return the net price so the POS adds the tax
        # forward to reach the promotion price.
        tax_included_mode = bool(tax.price_include or tax.pos_price_include)
        now = fields.Datetime.now()

        domain = [
            ('company_id', '=', company_id),
            ('product_id', 'in', product_ids),
            '|', ('date_start', '=', False), ('date_start', '<=', now),
            '|', ('date_end', '=', False), ('date_end', '>=', now),
        ]

        promotions = request.env['product.promotion.location'].sudo().search(domain)
        result = {}

        for promo in promotions:

            if promo.warehouse_ids:
                if not warehouse_id or warehouse_id not in promo.warehouse_ids.ids:
                    continue

            gross_price = promo.price_tax_included or 0.0

            if tax_included_mode or not tax_rate:
                # Feed the tax-included price as-is; the POS derives base/tax
                # backwards and shows exactly the promotion amount.
                net_price = gross_price
            else:
                net_price = gross_price / (1 + (tax_rate / 100))

            net_price = round(net_price, 4)

            product_id = promo.product_id.id

            promo_data = {
                "promotion_id": promo.id,
                "product_id": product_id,
                "price": net_price,
                "gross_price": gross_price,
                "pricelist_ids": promo.pricelist_ids.ids or False
            }

            if product_id not in result:
                result[product_id] = []

            result[product_id].append(promo_data)

        final_result = []
        for promos in result.values():
            final_result.extend(promos)

        return final_result
