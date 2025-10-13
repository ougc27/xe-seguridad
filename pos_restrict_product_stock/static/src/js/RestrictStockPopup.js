/** @odoo-module **/
/*
 * This file is used to store a popup for stocks out of stock for forced orders.
 */
import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";

class RestrictStockPopup extends AbstractAwaitablePopup {
    async _OrderProduct() {
    // On clicking order product button on popup, it will add product to orderline
        var product = this.env.services.pos.db.get_product_by_id(this.props.pro_id)
        const options = {
            quantity: 1,
            extras: {
                price_type: "manual",
            },
        }
        const newLine = await this.env.services.pos.selectedOrder.add_product(product, options);
        newLine.qty_available = 0;
        newLine.no_stock = true;
        this.cancel();
    }
}
RestrictStockPopup.template = 'RestrictStockPopup';
export default RestrictStockPopup;
