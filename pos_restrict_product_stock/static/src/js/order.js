/** @odoo-module */

import { Order } from "@point_of_sale/app/store/models";
import { patch } from "@web/core/utils/patch";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";


patch(Order.prototype, {
    async pay() {
        const invalidQty = this.orderlines.some(line => line.quantity === 0);
        if (!invalidQty) {
            return await super.pay();
        }
        await this.env.services.popup.add(ErrorPopup, {
            title: _t("Check quantities"),
            body: _t(
                "Quantity needs to be selected for one or more products."
            ),
        });
        return false;
    },
    export_as_JSON() {
        const result = super.export_as_JSON(...arguments);
        if (!result) {
            return result;
        }
        return Object.assign(result, {
            reference: this.reference,
        });
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.reference = json.reference || false;
    }
});
