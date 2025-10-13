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
            await this.recompute_price();
        });
    },
    async recompute_price() {
        const order = this.pos.selectedOrder;
        const lines = order.get_orderlines();
        if (!order.pos.config.tax_id || !lines) {
            return;
        }
    
        const payload = lines
            .filter(line => !String(line.damage_type || "").toLowerCase().includes("outlet"))
            .map(line => ({
                product_id: line.product.id,
                currency_id: order.pos.currency.id,
                pricelist_id: order.pricelist.id,
                quantity: line.quantity,
                taxes_id: order.pos.config.tax_id[0],
                original_price: line.original_price,
            }));

        if (!payload) {
            return;
        }
    
        try {
            const newPrices = await this.pos.env.services.orm.call(
                "product.pricelist",
                "get_product_pricelist_by_installment",
                [],
                { lines: payload, payment_plan: "cash" }
            );
    
            lines.forEach((line, idx) => {
                if (newPrices[idx]) {
                    line.set_unit_price(newPrices[idx].price);
                }
            });
    
            lines.forEach(line => {
                const promo = this.pos.promotions?.find(p => p.product_id === line.product.id);
                if (promo) {
                    line.set_unit_price(promo.price);
                }
            });
    
        } catch (error) {
            console.error("Error recomputing prices:", error);
        }
    },
    
    async _load_promotions() {
        try {
            const order = this.pos.selectedOrder;
            const product_ids = Object.keys(this.pos.db.product_by_id).map(id => parseInt(id));
            const company_id = this.pos.config.company_id[0];
            const warehouse_id = this.pos.config.warehouse_id[0];
            const original_pricelist_id = order.pricelist.id;
            const currency_id = this.pos.config.currency_id[0];
            let tax_id = null;
            if (this.pos.config.tax_id) {
                tax_id = this.pos.config.tax_id[0];
            }

            const promotions = await this.rpc("/pos/promotion_data", {
                company_id,
                warehouse_id,
                product_ids,
                original_pricelist_id,
                currency_id,
                tax_id
            });            

            this.pos.promotions = promotions;
            this._apply_promotions_to_products();

        } catch (error) {
            console.error("Error loading promotions:", error);
        }
    },
    _apply_promotions_to_products() {
        if (!this.pos.promotions) return;
    
        const promo_by_product = Object.fromEntries(
            this.pos.promotions.map(promo => [promo.product_id, promo])
        );
    
        Object.keys(promo_by_product).forEach(product_id => {
            const product = this.pos.db.product_by_id[product_id];
            if (product) {
                const promo = promo_by_product[product_id];
                product.original_price = product.lst_price;
                product.lst_price = promo.price;
                product.price_type = promo.price_type;
            }
        });
    },
    back() {
        const order = this.pos.get_order();
        if (order && order.payment_plan && order.payment_plan !== "cash") {
            this.showScreen("PaymentConfiguratorScreen");
        } else {
            super.back();
        }
    },
});
