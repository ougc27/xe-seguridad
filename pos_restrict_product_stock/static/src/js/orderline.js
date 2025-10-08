/** @odoo-module */

import { Orderline } from "@point_of_sale/app/store/models";
import {
    roundDecimals as round_di,
    floatIsZero,
} from "@web/core/utils/numbers";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";


patch(Orderline.prototype, {
    get_all_prices(qty = this.get_quantity()) {
        const price_unit = this.get_unit_price() * (1.0 - this.get_discount() / 100.0);
        let taxtotal = 0;

        const product = this.get_product();
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null; 
        let taxes_ids = posTaxId || this.tax_ids || product.taxes_id;
        taxes_ids = taxes_ids.filter((t) => t in this.pos.taxes_by_id);
        const taxdetail = {};
        const product_taxes = this.pos.get_taxes_after_fp(taxes_ids, this.order.fiscal_position);

        const all_taxes = this.compute_all(
            product_taxes,
            price_unit,
            qty,
            this.pos.currency.rounding
        );
        const all_taxes_before_discount = this.compute_all(
            product_taxes,
            this.get_unit_price(),
            qty,
            this.pos.currency.rounding
        );

        all_taxes.taxes.forEach(function (tax) {
            taxtotal += tax.amount;
            taxdetail[tax.id] = {
                amount: tax.amount,
                base: tax.base,
            };
        });

        let priceWithTax = all_taxes.total_included;
        if (priceWithTax) {
            const integer_part = Math.floor(priceWithTax);
            const decimals = priceWithTax - integer_part;
            priceWithTax = decimals >= 0.5 ? integer_part + 1 : integer_part;
        }

        return {
            priceWithTax: priceWithTax,
            priceWithoutTax: all_taxes.total_excluded,
            priceWithTaxBeforeDiscount: all_taxes_before_discount.total_included,
            priceWithoutTaxBeforeDiscount: all_taxes_before_discount.total_excluded,
            tax: taxtotal,
            taxDetails: taxdetail,
        };
    },
    get_applicable_taxes() {
        var i;
        // Shenaningans because we need
        // to keep the taxes ordering.
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null; 
        var ptaxes_ids = posTaxId || this.tax_ids || this.get_product().taxes_id;
        var ptaxes_set = {};
        for (i = 0; i < ptaxes_ids.length; i++) {
            ptaxes_set[ptaxes_ids[i]] = true;
        }
        var taxes = [];
        for (i = 0; i < this.pos.taxes.length; i++) {
            if (ptaxes_set[this.pos.taxes[i].id]) {
                taxes.push(this.pos.taxes[i]);
            }
        }
        return taxes;
    },
    get_taxes() {
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null; 
        var taxes_ids = posTaxId || this.tax_ids || this.get_product().taxes_id;
        return this.pos.getTaxesByIds(taxes_ids);
    },
    _getProductTaxesAfterFiscalPosition() {
        const product = this.get_product();
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null; 
        let taxesIds = posTaxId || this.tax_ids || product.taxes_id;
        taxesIds = taxesIds.filter((t) => t in this.pos.taxes_by_id);
        return this.pos.get_taxes_after_fp(taxesIds, this.order.fiscal_position);
    },
});
