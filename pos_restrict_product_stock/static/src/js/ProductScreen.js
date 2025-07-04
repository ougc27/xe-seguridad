/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import RestrictStockPopup from "@pos_restrict_product_stock/js/RestrictStockPopup"
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";


patch(PosStore.prototype, {

    async addProductToCurrentOrder(...args) {
        var type = this.config.stock_type
        const product_id = args['0'].id
        const [qty_available, virtual_available] = await this.getProductQuantity(product_id)
        if (this.config.is_restrict_product && ((type == 'qty_on_hand') && (qty_available <= 0)) | ((type == 'virtual_qty') && (virtual_available <= 0)) |
            ((args['0'].qty_available <= 0) && (args['0'].virtual_available <= 0))) {
            this.popup.add(RestrictStockPopup, {
                body: args['0'].display_name,
                pro_id: product_id
            });
        }    
        else{
            await super.addProductToCurrentOrder(...args);
            const orderlines = this.env.services.pos.selectedOrder.orderlines;
            orderlines.forEach(line => {
                if (line.product.id === product_id) {
                    line.qty_available = qty_available;
                    line.no_stock = false;
                }
            });
        }
    },
    async getProductQuantity(product_id) {
        const picking_type_id = this.env.services.pos.picking_type.id;
        return await this.orm.call("product.product", "get_product_quantity", [], {
            picking_id: picking_type_id,
            product_id: product_id
        });
    }
});
