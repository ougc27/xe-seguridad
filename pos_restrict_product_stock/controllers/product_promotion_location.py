from odoo import http, fields
from odoo.http import request

import logging
_logger = logging.getLogger(__name__)


class PosPromotionController(http.Controller):

    @http.route('/pos/promotion_data', type='json', auth='user')
    def get_promotion_data(self, company_id, warehouse_id, product_ids, original_pricelist_id=None, currency_id=None, tax_id=None):
        """
        Devuelve promociones aplicables (product_id, price, price_type)
        filtradas por compañía, fechas y warehouse.
        - Si hay más de una promo por producto, devuelve la de menor fixed_price
        - product_ids: lista opcional para filtrar productos específicos
        """
        now = fields.Datetime.now()

        product_filter_sql = ""
        params = [company_id, now, now, warehouse_id or 0]
        if product_ids:
            product_filter_sql = " AND ppl.product_id = ANY(%s)"
            params.append(product_ids)

        query = f"""
            SELECT ppl.id, ppl.product_id, ppl.price, ppl.price_type, ppl.pricelist_id
            FROM product_promotion_location ppl
            WHERE ppl.company_id = %s
              AND (ppl.date_start IS NULL OR ppl.date_start <= %s)
              AND (ppl.date_end IS NULL OR ppl.date_end >= %s)
              AND (
                    NOT EXISTS (
                        SELECT 1 FROM product_promotion_location_stock_warehouse_rel rel
                        WHERE rel.product_promotion_location_id = ppl.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM product_promotion_location_stock_warehouse_rel rel
                        WHERE rel.product_promotion_location_id = ppl.id
                          AND rel.stock_warehouse_id = %s
                    )
                  )
              {product_filter_sql}
        """

        request.env.cr.execute(query, params)
        rows = request.env.cr.fetchall()

        promotions_by_product = {}
        for ppl_id, product_id, fixed_price, price_type, pricelist_id in rows:
            fixed_price = float(fixed_price or 0.0)
            result = request.env['product.pricelist'].sudo().get_product_pricelist(product_id, currency_id, original_pricelist_id, 1, tax_id, 'no_outlet')
            original_price = 0
            if result:
                original_price = result['price_included']
                integer_part = int(original_price)
                decimals = original_price - integer_part
                if decimals >= 0.5:
                    original_price = integer_part + 1
                else:
                    original_price = integer_part
            if product_id not in promotions_by_product:
                promotions_by_product[product_id] = {
                    "promotion_id": ppl_id,
                    "product_id": product_id,
                    "price": fixed_price,
                    "price_type": price_type or "fixed",
                    "original_price": original_price,
                    "pricelist_id": pricelist_id
                }
            else:
                if fixed_price < promotions_by_product[product_id]["price"]:
                    promotions_by_product[product_id] = {
                        "promotion_id": ppl_id,
                        "product_id": product_id,
                        "price": fixed_price,
                        "price_type": price_type or "fixed",
                        "original_price": original_price,
                        "pricelist_id": pricelist_id
                    }

        return list(promotions_by_product.values())
