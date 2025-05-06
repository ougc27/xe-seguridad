/** @odoo-module */

import { ClosePosPopup } from "@point_of_sale/app/navbar/closing_popup/closing_popup";
import { patch } from "@web/core/utils/patch";


patch(ClosePosPopup.prototype, {
    getInitialState() {
        const initialState = { notes: "", payments: {} };
        if (this.pos.config.cash_control) {
            initialState.payments[this.props.default_cash_details.id] = {
                counted: this.env.utils.formatCurrency(this.props.default_cash_details.amount, false),
            };
        }
        this.props.other_payment_methods.forEach((pm) => {
            if (pm.type === "bank") {
                initialState.payments[pm.id] = {
                    counted: this.env.utils.formatCurrency(pm.amount, false),
                };
            }
        });
        return initialState;
    }
});
