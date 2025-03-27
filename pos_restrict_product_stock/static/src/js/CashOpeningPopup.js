/** @odoo-module */

import { CashOpeningPopup } from "@point_of_sale/app/store/cash_opening_popup/cash_opening_popup";
import { patch } from "@web/core/utils/patch";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";


patch(CashOpeningPopup.prototype, {
    async confirm() {
        const closingCash = this.pos.config.last_session_closing_cash
        const openingCash = this.state.openingCash;
        if (closingCash != openingCash) {
            this.popup.add(ErrorPopup, {
                title: _t("The opening cash must be equal to the previous day's closing cash."),
                body: _t(
                    `El efectivo al cierre del día anterior es ${closingCash}, y está aperturando con ${openingCash} de efectivo.`
                ),
            });
            return false;
        }
        super.confirm();
        
    }
});
