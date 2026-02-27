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

            if tax_rate:
                net_price = gross_price / (1 + (tax_rate / 100))
            else:
                net_price = gross_price

            net_price = round(net_price, 4)

            product_id = promo.product_id

            if product_id not in result:
                result[product_id] = {
                    "promotion_id": promo.id,
                    "product_id": product_id.id,
                    "price": net_price,
                    "gross_price": gross_price,
                    "pricelist_ids": promo.pricelist_ids.ids or False
                }
            else:
                if net_price < result[product_id]["price"]:
                    result[product_id] = {
                        "promotion_id": promo.id,
                        "product_id": product_id,
                        "price": net_price,
                        "gross_price": gross_price,
                        "pricelist_ids": promo.pricelist_ids.ids or False
                    }

        return list(result.values())
