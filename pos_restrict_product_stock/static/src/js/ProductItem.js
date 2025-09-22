/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks"
import { useState } from "@odoo/owl";
import { ProductCard } from "@point_of_sale/app/generic_components/product_card/product_card";
import { usePos } from "@point_of_sale/app/store/pos_hook";

patch(ProductCard.prototype, {

    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.pos = usePos();
        this.state = useState({
            qtyAvailable: 0,
            virtualAvailable: 0,
            originalPrice: 0
        });
        this.getProductQuantity(this.props.productId);
        this.setOriginalPrice(this.props.productId)
    },
    
    async getProductQuantity(product_id) {
        const picking_type_id = this.env.services.pos.picking_type.id;
        const [qty_available, virtual_available] = await this.orm.call("product.product", "get_product_quantity", [], {
            picking_id: picking_type_id,
            product_id: product_id
        });
        this.state.qtyAvailable = qty_available;
        this.state.virtualAvailable = virtual_available;
    },
    async setOriginalPrice(product_id) {
        if (this.pos.promotions) {
            const promo = this.pos.promotions.find(p => p.product_id === product_id);
            if (promo) {
                this.state.originalPrice = promo.original_price;
            }
        }
    },
});
