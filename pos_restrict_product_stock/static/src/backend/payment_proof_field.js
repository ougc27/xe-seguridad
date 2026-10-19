/** @odoo-module **/

import { registry } from "@web/core/registry";
import { formatDateTime } from "@web/core/l10n/dates";
import { many2manybinarypreviewfield } from "@ps_m2m_field_attachment_preview/js/m2m_field_preview";

class PaymentProofField extends many2manybinarypreviewfield.component {
    static template = "pos_restrict_product_stock.PaymentProofField";

    getUploadDate(file) {
        return file.create_date ? formatDateTime(file.create_date) : "";
    }
}

registry.category("fields").add("pos_payment_proof", {
    ...many2manybinarypreviewfield,
    component: PaymentProofField,
    relatedFields: [
        ...many2manybinarypreviewfield.relatedFields,
        { name: "create_date", type: "datetime" },
    ],
});
