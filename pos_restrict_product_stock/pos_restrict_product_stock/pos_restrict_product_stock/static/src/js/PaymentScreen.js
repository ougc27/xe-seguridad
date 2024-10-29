/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { _t } from "@web/core/l10n/translation";


patch(PaymentScreen.prototype, {
    async _isOrderValid(isForceValidate) {
        if (this.currentOrder.get_orderlines().length === 0 && this.currentOrder.is_to_invoice()) {
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

        if (
            (this.currentOrder.is_to_invoice() || this.currentOrder.getShippingDate()) &&
            !this.pos.config.delivery_partner_id
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

        //let partner = this.currentOrder.get_partner();
        if (
            this.currentOrder.getShippingDate()
        ) {
            const partnerId = this.pos.config.delivery_partner_id[0];
            const partnerData=  await this.orm.call("pos.order", "get_res_partner_by_id", [partnerId]);
            const [name, street, city, country_id] = partnerData;

            if (!(name && street && city && country_id)) {
                this.popup.add(ErrorPopup, {
                    title: _t("Incorrect address for shipping"),
                    body: _t("The selected customer needs an address."),
                });
                return false;
            }
        }

        if (
            this.currentOrder.get_total_with_tax() != 0 &&
            this.currentOrder.get_paymentlines().length === 0
        ) {
            this.notification.add(_t("Select a payment method to validate the order."));
            return false;
        }

        if (!this.currentOrder.is_paid() || this.invoicing) {
            return false;
        }

        if (this.currentOrder.has_not_valid_rounding()) {
            var line = this.currentOrder.has_not_valid_rounding();
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
                this.currentOrder.get_total_with_tax() -
                    this.currentOrder.get_total_paid() +
                    this.currentOrder.get_rounding_applied()
            ) > 0.00001
        ) {
            if (!this.pos.payment_methods.some((pm) => pm.is_cash_count)) {
                this.popup.add(ErrorPopup, {
                    title: _t("Cannot return change without a cash payment method"),
                    body: _t(
                        "There is no cash payment method available in this point of sale to handle the change.\n\n Please pay the exact amount or add a cash payment method in the point of sale configuration"
                    ),
                });
                return false;
            }
        }

        // if the change is too large, it's probably an input error, make the user confirm.
        if (
            !isForceValidate &&
            this.currentOrder.get_total_with_tax() > 0 &&
            this.currentOrder.get_total_with_tax() * 1000 < this.currentOrder.get_total_paid()
        ) {
            this.popup
                .add(ConfirmPopup, {
                    title: _t("Please Confirm Large Amount"),
                    body:
                        _t("Are you sure that the customer wants to  pay") +
                        " " +
                        this.env.utils.formatCurrency(this.currentOrder.get_total_paid()) +
                        " " +
                        _t("for an order of") +
                        " " +
                        this.env.utils.formatCurrency(this.currentOrder.get_total_with_tax()) +
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

        if (!this.currentOrder._isValidEmptyOrder()) {
            return false;
        }

        return true;
    }
});
