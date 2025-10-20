/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { _t } from "@web/core/l10n/translation";


patch(PaymentScreen.prototype, {
    async _isOrderValid(isForceValidate) {
        const order = this.currentOrder;
        if (order.get_orderlines().length === 0 && order.is_to_invoice()) {
            this.popup.add(ErrorPopup, {
                title: _t("Empty Order"),
                body: _t(
                    "There must be at least one product in your order before it can be validated and invoiced."
                ),
            });
            return false;
        }

        if (await this._askForCustomerIfRequired() === false) {
            return false;
        }

        if (!order.getShippingDate()) {
            const insufficientProducts = [];

            order.orderlines.forEach(line => {
                if (line.coupon_id) {
                    return;
                }
                const qtyOrdered = line.quantity;
                const qtyAvailable = line.qty_available || 0;
                if (qtyAvailable < qtyOrdered) {
                    insufficientProducts.push({
                        productName: line.product.display_name,
                        qtyOrdered: qtyOrdered,
                        qtyAvailable: qtyAvailable,
                    });
                }
            });

            if (insufficientProducts.length > 0) {
                const msg = insufficientProducts.map(p =>
                    `${p.productName}: ordenado ${p.qtyOrdered}, cantidad disponible ${p.qtyAvailable}`
                ).join('\n');

                this.popup.add(ErrorPopup, {
                    title: _t("Insufficient Stock for Immediate Delivery"),
                    body: _t(
                        "The following products do not have enough stock for immediate delivery:\n\n%s",
                        msg
                    ),
                });

                return;
            }
            const { confirmed } = await this.popup.add(ConfirmPopup, {
                title: _t("Delivery Method"),
                body: _t(
                    "Are you sure the customer is taking the product right now?"
                ),
            });
            if (!confirmed) {
                return false;
            }
        }

        if (
            (order.is_to_invoice() || order.getShippingDate()) &&
            !order.get_partner()
        ) {
            const { confirmed } = await this.popup.add(ConfirmPopup, {
                title: _t("Please select the Customer"),
                body: _t(
                    "You need to select the customer before you can invoice or ship an order."
                ),
            });
            if (confirmed) {
                this.selectPartner();
            }
            return false;
        }

        const partner = order.get_partner();
        if (
            order.getShippingDate() &&
            !(partner.name && partner.street && partner.city && partner.country_id)
        ) {
            this.popup.add(ErrorPopup, {
                title: _t("Incorrect address for shipping"),
                body: _t("The selected customer needs an address."),
            });
            return false;
        }

        if (
            order.get_total_with_tax() != 0 &&
            order.get_paymentlines().length === 0
        ) {
            this.notification.add(_t("Select a payment method to validate the order."));
            return false;
        }

        if (!order.is_paid() || this.invoicing) {
            return false;
        }

        if (order.has_not_valid_rounding()) {
            var line = order.has_not_valid_rounding();
            this.popup.add(ErrorPopup, {
                title: _t("Incorrect rounding"),
                body: _t(
                    "You have to round your payments lines." + line.amount + " is not rounded."
                ),
            });
            return false;
        }

        // The exact amount must be paid if there is no cash payment method defined.
        if (
            Math.abs(
                order.get_total_with_tax() -
                    order.get_total_paid() +
                    order.get_rounding_applied()
            ) > 0.00001
        ) {
            /*if (!this.pos.payment_methods.some((pm) => pm.is_cash_count)) {
                this.popup.add(ErrorPopup, {
                    title: _t("Cannot return change without a cash payment method"),
                    body: _t(
                        "There is no cash payment method available in this point of sale to handle the change.\n\n Please pay the exact amount or add a cash payment method in the point of sale configuration"
                    ),
                });
                return false;
            }*/
            await this.popup.add(ErrorPopup, {
                title: _t("Incorrect Total Payment"),
                body: _t(
                    "The total amount must exactly match the order total."
                ),
            });
            return false;
        }

        for (const line of order.get_paymentlines()) {
            const amount = line.get_amount();

            if (amount < 0) {
                await this.popup.add(ErrorPopup, {
                    title: _t("Invalid Payment Amount"),
                    body: _t(
                        "A payment method cannot have a negative amount. Please check your entries."
                    ),
                });
                return false;
            }

            /*if (amount > order.get_total_with_tax()) {
                await this.popup.add(ErrorPopup, {
                    title: _t("Excessive Payment"),
                    body: _t(
                        `El método de pago "${line.payment_method.name}" excede el monto total del pedido.`
                    ),
                });
                return false;
            }*/
        }

        // if the change is too large, it's probably an input error, make the user confirm.
        if (
            !isForceValidate &&
            order.get_total_with_tax() > 0 &&
            order.get_total_with_tax() * 1000 < order.get_total_paid()
        ) {
            this.popup
                .add(ConfirmPopup, {
                    title: _t("Please Confirm Large Amount"),
                    body:
                        _t("Are you sure that the customer wants to  pay") +
                        " " +
                        this.env.utils.formatCurrency(order.get_total_paid()) +
                        " " +
                        _t("for an order of") +
                        " " +
                        this.env.utils.formatCurrency(order.get_total_with_tax()) +
                        " " +
                        _t('? Clicking "Confirm" will validate the payment.'),
                })
                .then(({ confirmed }) => {
                    if (confirmed) {
                        this.validateOrder(true);
                    }
                });
            return false;
        }

        if (!order._isValidEmptyOrder()) {
            return false;
        }

        return true;
    },

    referenceKeyup (ev) {
        const order = this.currentOrder;
        order.reference = ev.target.value;
    },

    onlyNumbers (ev) {
        const keys = [
            "Enter", "Tab", "Backspace", "ArrowLeft",
            "ArrowRight", "Control", "Shift",
        ];
        if (keys.includes(ev.key)) {
            return true;
        }
        if (ev.key === ' ' || isNaN(ev.key)) {
            ev.preventDefault();
        }
    }
});
