/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { Component } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";


export class CouponButton extends Component {
    static template = "your_module.CouponButton";

    setup() {
        this.pos = usePos();
        this.popup = useService("popup");
    }

    async onClick() {
        const order = this.pos.get_order();
        const line = order?.get_selected_orderline();
        if (!line) return;

        const productCoupons = line.product.loyalty_cards || [];
        const currentCoupon = line.coupon_id;
        const list = [];

        if (productCoupons.length) {
            for (const coupon of productCoupons) {
                list.push({
                    id: coupon.id,
                    label: coupon.code,
                    description: coupon.program_name,
                    item: coupon,
                });
            }
        }

        list.push({
            id: "manual",
            label: "Enter coupon manually",
            item: "manual",
        });

        if (currentCoupon) {
            list.unshift({
                id: "remove",
                label: "— Remove Coupon —",
                item: "remove",
            });
        }

        const { confirmed, payload } = await this.popup.add(SelectionPopup, {
            title: _t("Coupon"),
            list,
        });

        if (!confirmed) return;

        if (payload === "remove") {
            line.removeCoupon();
            return;
        }

        if (payload === "manual") {
            const { confirmed: confirmText, payload: text } =
                await this.popup.add(TextInputPopup, {
                    title: _t("Enter Coupon Code"),
                    placeholder: "Coupon code...",
                });

            if (!confirmText || !text) return;

            const result = await order.addCouponPerLine(text, line);
            if (!result) return;

            line.set_unit_price(result.price_with_discount);
            line.setCoupon(result);
            return;
        }

        if (payload.id === line.coupon_id) {
            return;
        }

        if (line.coupon_id) {
            line.removeCoupon();
        }

        const result = await order.addCouponPerLine(payload.code, line);
        if (!result) return;

        const alreadyUsed = order
            .get_orderlines()
            .some(line => line.coupon_id === result.coupon_id); 
        if (alreadyUsed) {
            await this.popup.add(ErrorPopup, {
                title: _t("Coupon"),
                body: _t("Coupon has already been applied to this order."),
            });
            return;
        }
        line.set_unit_price(result.price_with_discount);
        line.setCoupon(result);
    }
}

ProductScreen.addControlButton({
    component: CouponButton,
    condition: function () {
        return true;
    },
});