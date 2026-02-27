/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks"
import { useState, useEffect } from "@odoo/owl";
import { ProductCard } from "@point_of_sale/app/generic_components/product_card/product_card";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { onMounted } from "@odoo/owl";

patch(ProductCard.prototype, {

    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.state = useState({
            qtyAvailable: 0,
            virtualAvailable: 0
        });
        this.getProductQuantity(this.props.productId);
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
});
