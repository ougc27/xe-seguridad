/** @odoo-module */

import { Product } from "@point_of_sale/app/store/models";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import {
    deserializeDate,
} from "@web/core/l10n/dates";

const { DateTime } = luxon;

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
    },

    get_price(pricelist, quantity, price_extra = 0, recurring = false) {
        const date = DateTime.now();

        // In case of nested pricelists, it is necessary that all pricelists are made available in
        // the POS. Display a basic alert to the user in the case where there is a pricelist item
        // but we can't load the base pricelist to get the price when calling this method again.
        // As this method is also call without pricelist available in the POS, we can't just check
        // the absence of pricelist.
        if (recurring && !pricelist) {
            alert(
                _t(
                    "An error occurred when loading product prices. " +
                        "Make sure all pricelists are available in the POS."
                )
            );
        }
        const rules = !pricelist
            ? []
            : (this.applicablePricelistItems[pricelist.id] || []).filter((item) =>
                  this.isPricelistItemUsable(item, date)
              );
        
        let price = this.lst_price + (price_extra || 0);
        const rule = rules.find((rule) => !rule.min_quantity || quantity >= rule.min_quantity);
        if (!rule) {
            return price;
        }

        if (rule.base === "pricelist") {
            const pricelists = this.pos.all_pricelists || this.pos.pricelists;
            const base_pricelist = pricelists.find(
                (pricelist) => pricelist.id === rule.base_pricelist_id[0]
            );
            if (base_pricelist) {
                price = this.get_price(base_pricelist, quantity, 0, true);
            }
        } else if (rule.base === "standard_price") {
            price = this.standard_price;
        }

        if (rule.compute_price === "fixed") {
            price = rule.fixed_price;
        } else if (rule.compute_price === "percentage") {
            price = price - price * (rule.percent_price / 100);
        } else {
            var price_limit = price;
            price -= price * (rule.price_discount / 100);
            if (rule.price_round) {
                price = round_pr(price, rule.price_round);
            }
            if (rule.price_surcharge) {
                price += rule.price_surcharge;
            }
            if (rule.price_min_margin) {
                price = Math.max(price, price_limit + rule.price_min_margin);
            }
            if (rule.price_max_margin) {
                price = Math.min(price, price_limit + rule.price_max_margin);
            }
        }

        // This return value has to be rounded with round_di before
        // being used further. Note that this cannot happen here,
        // because it would cause inconsistencies with the backend for
        // pricelist that have base == 'pricelist'.
        return price;
    },
});
