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
        if (this.pos.promotions && this.id) {
            const promo = this.pos.promotions.find(p => p.product_id === this.id);
            if (promo) {
                this.original_price = price;    
                price = promo.price;
            }
        }
    
        const order = this.pos.get_order();
        const taxes = this.pos.get_taxes_after_fp(this.taxes_id, order && order.fiscal_position);
        const currentTaxes = this.pos.getTaxesByIds(this.taxes_id);
        const priceAfterFp = this.pos.computePriceAfterFp(price, currentTaxes);
        const allPrices = this.pos.compute_all(taxes, priceAfterFp, quantity, this.pos.currency.rounding);
    
        if (iface_tax_included === "total") {
            return allPrices.total_included;
        } else {
            return allPrices.total_excluded;
        }
    },
});
