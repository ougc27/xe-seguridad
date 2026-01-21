/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { _t } from "@web/core/l10n/translation";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";


patch(ProductScreen.prototype, {
    getNumpadButtons() {
        return [
            { value: "1" },
            { value: "2" },
            { value: "3" },
            { value: "quantity", text: _t("Qty") },
            { value: "4" },
            { value: "5" },
            { value: "6" },
            { value: "coupon", text: _t("Coupon") },
            { value: "7" },
            { value: "8" },
            { value: "9" },
            {
                value: "price",
                text: _t("Price"),
                disabled: !this.pos.cashierHasPriceControlRights(),
            },
            { value: "-", text: "+/-" },
            { value: "0" },
            { value: this.env.services.localization.decimalPoint },
            // Unicode: https://www.compart.com/en/unicode/U+232B
            { value: "Backspace", text: "⌫" },
        ].map((button) => ({
            ...button,
            class: this.pos.numpadMode === button.value ? "active border-primary" : "",
        }));
    },
    async onNumpadClick(buttonValue) {
        if (buttonValue === "coupon") {
            await this._onClickCoupon();
            return;
        }
        return super.onNumpadClick(buttonValue);
    },
    async _onClickCoupon() {
        const order = this.currentOrder;
        const line = order.get_selected_orderline();
        if (!line) {
            await this.popup.add(ErrorPopup, {
                title: _t("Coupon"),
                body: _t("Please select a line before applying a coupon."),
            });
            return;
        }

        if (line.coupon_id) {
            const { confirmed } = await this.popup.add(ConfirmPopup, {
                title: _t("Are you sure you want to remove the coupon?"),
                body: _t(
                    "Removing this coupon will remove the discount from the order. Do you want to proceed?"
                ),
                confirmText: _t("Yes"),
                cancelText: _t("No"),
            });
            if (confirmed) {
                return line.removeCoupon();
            }
            return;
        }
    
        let { confirmed, payload: code } = await this.popup.add(TextInputPopup, {
            title: _t("Enter Code"),
            startingValue: "",
            placeholder: _t("Gift card or Discount code"),
        });
        if (confirmed) {
            code = code.trim();
            if (code !== "") {
                const result = await order.addCouponPerLine(code, line);
                if (!result) {
                    return;
                }
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
    },
    async _setValue(val) {
        const { numpadMode } = this.pos;
        let selectedLine = this.currentOrder.get_selected_orderline();
        if (selectedLine) {
            if (numpadMode === "quantity") {
                if (selectedLine.coupon_id) {
                    const { confirmed } = await this.env.services.popup.add(ConfirmPopup, {
                        title: _t("Change quantity"),
                        body: _t(
                            "At least one order line has an applied coupon. " +
                            "Changing the quantity will remove all coupons and you will need to apply them again. " +
                            "Do you want to continue?"
                        ),
                        confirmText: _t("Yes, change quantity"),
                        cancelText: _t("Cancel"),
                    });
                    if (!confirmed) {
                        this.numberBuffer.reset();
                        return;
                    }
                    selectedLine.removeCoupon();
                }
                if (selectedLine.comboParent) {
                    selectedLine = selectedLine.comboParent;
                }
                if (val === "remove") {
                    this.currentOrder.removeOrderline(selectedLine);
                } else {
                    const result = selectedLine.set_quantity(
                        val,
                        Boolean(selectedLine.comboLines?.length)
                    );
                    if (selectedLine.comboLines) {
                        for (const line of selectedLine.comboLines) {
                            line.set_quantity(val, true);
                        }
                    }
                    if (!result) {
                        this.numberBuffer.reset();
                    }
                }
            } else if (numpadMode === "discount") {
                this.pos.setDiscountFromUI(selectedLine, val);
            } else if (numpadMode === "price") {
                selectedLine.price_type = "manual";
                selectedLine.set_unit_price(val);
            }
        }
    }
});
