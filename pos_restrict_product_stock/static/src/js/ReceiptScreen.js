/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { ReceiptScreen } from "@point_of_sale/app/screens/receipt_screen/receipt_screen";
import { _t } from "@web/core/l10n/translation";
import { OrderReceipt } from "@point_of_sale/app/screens/receipt_screen/receipt/order_receipt";

patch(ReceiptScreen.prototype, {
    async sendToCustomer(orderPartner, methodName) {
        const ticketImage = await this.renderer.toJpeg(
            OrderReceipt,
            {
                data: this.pos.get_order().export_for_printing(),
                formatCurrency: this.env.utils.formatCurrency,
            },
            { addClass: "pos-receipt-print",
              addEmailMargins: true,
             }
        );
        const order = this.currentOrder;
        const orderName = order.get_name();
        const order_server_id = this.pos.validated_orders_name_server_id_map[orderName];
        /*if (!order_server_id) {
            this.popup.add(OfflineErrorPopup, {
                title: _t("Unsynced order"),
                body: _t(
                    "This order is not yet synced to server. Make sure it is synced then try again."
                ),
            });
            return Promise.reject();
        }*/
        await this.orm.call("pos.order", methodName, [
            [order_server_id],
            orderName,
            orderPartner,
            ticketImage,
        ]);
    }
});
