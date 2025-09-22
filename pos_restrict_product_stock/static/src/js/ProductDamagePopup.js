/** @odoo-module **/

import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";


export class ProductDamagePopup extends AbstractAwaitablePopup {
    setup() {
        super.setup();
        this.state = useState({
            quantity: 1,
            damage_type: null,
            price_excluded: 0,
            price_included: 0

        });
        this.orm = useService("orm");
        this.popup = useService("popup");
    }

    increment() {
        this.state.quantity += 1;
    }

    decrement() {
        if (this.state.quantity > 1) {
            this.state.quantity -= 1;
        }
    }

    async selectDamage(ev) {
        this.state.damage_type = null;
        const damage_type = ev.target.value;
        let promotion_price = 0;
        const product = this.env.services.pos.db.get_product_by_id(this.props.pro_id);
        if (product.original_price && damage_type == 'no_outlet') {
            promotion_price = product.lst_price;
        }        
        const priceData = await this.orm.call("product.pricelist", "get_product_pricelist", [], {
            product_id: this.props.pro_id,
            currency_id: this.props.currency_id,
            pricelist_id: this.props.pricelist_id,
            quantity: this.state.quantity,
            taxes_id: this.props.taxes_id,
            damage_type: damage_type,
            promotion_price: promotion_price
        });
        this.state.damage_type = damage_type;
        this.state.price_excluded = priceData.price_excluded;
        this.state.price_included = priceData.price_included;
    }

    async _OrderProduct() {
        if (!this.state.damage_type) {
            this.popup.add(ErrorPopup, {
                title: _t("Select Damage Type"),
                body: _t("You must select a damage type before adding the product."),
            });
            return;
        }

        if (this.state.quantity > this.props.qty_on_hand && this.state.damage_type != 'no_outlet') {
            this.popup.add(ErrorPopup, {
                title: _t("Quantity exceeds available stock for Outlet products"),
                body: _t("You cannot sell more units than the stock available in the store."),
            });            
            return;
        }

        const product = this.env.services.pos.db.get_product_by_id(this.props.pro_id);

        const options = {
            price: this.state.price_excluded,
            quantity: this.state.quantity,
            extras: {
                price_type: "manual",
            },
        }

        const newLine = await this.env.services.pos.selectedOrder.add_product(product, options);
        newLine.damage_type = this.state.damage_type;
        this.cancel();
    }
}

ProductDamagePopup.template = "ProductDamagePopup";
export default ProductDamagePopup;
