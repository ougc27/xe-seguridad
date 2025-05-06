/** @odoo-module */

import { CashMovePopup } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_popup";
import { CashMoveReceipt } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_receipt/cash_move_receipt";
import { parseFloat } from "@web/views/fields/parsers";
import { patch } from "@web/core/utils/patch";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";


patch(CashMovePopup.prototype, {
    setup() {
        super.setup();
        this.setDefaultAmount();
    },

    async setDefaultAmount() {
        try {
            let currentCash = await this.get_current_cash();
            currentCash = currentCash.toString()
            this.state.amount = currentCash;
            this.state.currentCash = parseFloat(currentCash);
        } catch (error) {
            this.notification.add(_t("Could not retrieve the current cash."), 3000);
        }
    },

    async get_current_cash() {
        return await this.orm.call("pos.session", "get_current_cash", [[this.pos.pos_session.id]]);
    },

    async confirm() {
        const amount = parseFloat(this.state.amount);
        const formattedAmount = this.env.utils.formatCurrency(amount);
        if (!amount) {
            this.notification.add(_t("Cash in/out of %s is ignored.", formattedAmount), 3000);
            return this.props.close();
        }

        if (amount <= 0) {
            this.popup.add(ErrorPopup, {
                title: _t("Invalid Amount"),
                body: _t(
                    "The cash out amount must be a positive value."
                ),
            });
            return;
        }

        if (amount !== this.state.currentCash) {
            this.popup.add(ErrorPopup, {
                title: _t("Cash Mismatch"),
                body: _t(
                    "The withdrawal amount must match the available cash in the register.\nCurrent available cash: %s",
                    this.state.currentCash
                ),
            });
            return;
        }

        const type = this.state.type;
        const translatedType = _t(type);
        const extras = { formattedAmount, translatedType };
        const reason = this.state.reason.trim();
        await this.orm.call("pos.session", "try_cash_in_out", [
            [this.pos.pos_session.id],
            type,
            amount,
            reason,
            extras,
        ]);
        await this.pos.logEmployeeMessage(
            `${_t("Cash")} ${translatedType} - ${_t("Amount")}: ${formattedAmount}`,
            "CASH_DRAWER_ACTION"
        );
        await this.printer.print(CashMoveReceipt, {
            reason,
            translatedType,
            formattedAmount,
            headerData: this.pos.getReceiptHeaderData(),
            date: new Date().toLocaleString(),
        });
        this.props.close();
        this.notification.add(
            _t("Successfully made a cash %s of %s.", type, formattedAmount),
            3000
        );
    }
});
