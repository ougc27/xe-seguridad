from odoo import api, models
from odoo.tools.float_utils import float_compare, float_is_zero


class StockForecastedProductProduct(models.AbstractModel):
    _inherit = "stock.forecasted_product_product"

    def _get_report_data(self, product_template_ids=False, product_ids=False):
        assert product_template_ids or product_ids
        res = {}

        warehouse = (
            self.env['stock.warehouse']
            .browse(self.env['stock.warehouse']._get_warehouse_id_from_context())
            or self.env['stock.warehouse'].search([['active', '=', True]])[0]
        )
        wh_location_ids = [
            loc['id']
            for loc in self.env['stock.location'].search_read(
                [('id', 'child_of', warehouse.view_location_id.id)], ['id']
            )
        ]
        wh_stock_location = warehouse.lot_stock_id

        res.update(self._get_report_header(product_template_ids, product_ids, wh_location_ids))
        res['lines'] = self.sudo()._get_report_lines(product_template_ids, product_ids, wh_location_ids, wh_stock_location)

        return res
