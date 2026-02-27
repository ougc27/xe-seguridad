/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";


patch(ProductScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.rpc = useService("rpc");
        onWillStart(async () => {
            await this._load_promotions();
        });
    },

    async _load_promotions() {
        try {
            const order = this.pos.selectedOrder;
            const product_ids = Object.keys(this.pos.db.product_by_id).map(id => parseInt(id));
            const company_id = this.pos.config.company_id[0];
            const picking_type_id = this.pos.config.picking_type_id[0];
            const original_pricelist_id = order.pricelist.id;
            const currency_id = this.pos.config.currency_id[0];
            let tax_id = null;
            if (this.pos.config.tax_id) {
                tax_id = this.pos.config.tax_id[0];
            }

            const promotions = await this.rpc("/pos/promotion_data", {
                company_id,
                picking_type_id,
                product_ids,
                original_pricelist_id,
                currency_id,
                tax_id
            });            

            this.pos.promotions = promotions;
            //this._apply_promotions_to_products();

        } catch (error) {
            console.error("Error loading promotions:", error);
        }
    },
});
