/** @odoo-module */

import { Order } from "@point_of_sale/app/store/models";
import { Orderline } from "@point_of_sale/app/store/models";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { patch } from "@web/core/utils/patch";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";


patch(Order.prototype, {
    async pay() {
        if (!this.canPay()) {
            return;
        }
        const invalidQty = this.orderlines.some(line => line.quantity === 0);
        if (invalidQty) {
            await this.env.services.popup.add(ErrorPopup, {
                title: _t("Check quantities"),
                body: _t("Quantity needs to be greater than 0 for all products."),
            });
            return false;
        }
        const invalidPrice = this.orderlines.some(line => line.get_unit_price() <= 0);
        if (invalidPrice) {
            await this.env.services.popup.add(ErrorPopup, {
                title: _t("Check prices"),
                body: _t("Check order lines: some items have a price of 0 or less."),
            });
            return false;
        }
        if (
            this.orderlines.some(
                (line) => line.get_product().tracking !== "none" && !line.has_valid_product_lot()
            ) &&
            (this.pos.picking_type.use_create_lots || this.pos.picking_type.use_existing_lots)
        ) {
            const { confirmed } = await this.env.services.popup.add(ConfirmPopup, {
                title: _t("Some Serial/Lot Numbers are missing"),
                body: _t(
                    "You are trying to sell products with serial/lot numbers, but some of them are not set.\nWould you like to proceed anyway?"
                ),
                confirmText: _t("Yes"),
                cancelText: _t("No"),
            });
            if (confirmed) {
                this.pos.mobile_pane = "right";
                this.env.services.pos.showScreen("PaymentScreen");
            }
        } else {
            this.pos.mobile_pane = "right";
            this.env.services.pos.showScreen("PaymentScreen");
        }
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
    },
    updatePricelistAndFiscalPosition(newPartner) {
        let newPartnerFiscalPosition;
        const defaultFiscalPosition = this.pos.fiscal_positions.find(
            (position) => position.id === this.pos.config.default_fiscal_position_id[0]
        );
        if (newPartner) {
            newPartnerFiscalPosition = newPartner.property_account_position_id
                ? this.pos.fiscal_positions.find(
                      (position) => position.id === newPartner.property_account_position_id[0]
                  )
                : defaultFiscalPosition;
        } else {
            newPartnerFiscalPosition = defaultFiscalPosition;
        }
        this.set_fiscal_position(newPartnerFiscalPosition);
    },
});
