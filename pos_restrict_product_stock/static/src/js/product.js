/** @odoo-module */

import { Product } from "@point_of_sale/app/store/models";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";


patch(Product.prototype, {
    get_display_price({
        pricelist = this.pos.getDefaultPricelist(),
        quantity = 1,
        price = this.get_price(pricelist, quantity),
        iface_tax_included = this.pos.config.iface_tax_included,
    } = {}) {
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null;
        let taxId = posTaxId || this.taxes_id;
        const order = this.pos.get_order();
        const taxes = this.pos.get_taxes_after_fp(taxId, order && order.fiscal_position);
        const currentTaxes = this.pos.getTaxesByIds(taxId);
        const priceAfterFp = this.pos.computePriceAfterFp(price, currentTaxes);
        const allPrices = this.pos.compute_all(taxes, priceAfterFp, quantity, this.pos.currency.rounding);
        if (iface_tax_included === "total") {
            return allPrices.total_included;
        } else {
            return allPrices.total_excluded;
        }
    }
});
