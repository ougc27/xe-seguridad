/** @odoo-module */

import { Orderline } from "@point_of_sale/app/store/models";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";

patch(Orderline.prototype, {
    setup() {
        super.setup(...arguments);
        this.coupon_id = this.coupon_id || null;
        this.coupon_code = this.coupon_code || null;
        this.discount_price = this.discount_price || null;
        this.discount_promotion = this.discount_promotion || null;
        this.price_with_discount = this.price_with_discount || null;
        this.price_without_discount = this.price_without_discount || null;
        this.promotion_id = this.promotion_id || null;
        this.promotion_price = this.promotion_price || null;
        this.original_price = this.original_price || null;
        this.available_promotion_pricelists = this.available_promotion_pricelists || null;
    },

    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.coupon_id = json.coupon_id || null;
        this.coupon_code = json.coupon_code || null;
        this.discount_price = json.discount_price || null;
        this.discount_promotion = json.discount_promotion || null;
        this.price_with_discount = json.price_with_discount || null;
        this.price_without_discount = json.price_without_discount || null;
        this.promotion_id = json.promotion_id || null;
        this.promotion_price = json.promotion_price || null;
        this.original_price = json.original_price || null;
        this.available_promotion_pricelists = json.available_promotion_pricelists || null;
        if (this.promotion_price && !this.coupon_id && !this.price_with_discount) {
            return super.set_unit_price(this.promotion_price);
        }
        if (this.coupon_id && this.price_with_discount) {
            return super.set_unit_price(this.price_with_discount);
        }
    },

    export_as_JSON() {
        const result = super.export_as_JSON(...arguments);
        result.coupon_id = this.coupon_id || false;
        result.coupon_code = this.coupon_code || false;
        result.discount_price = this.discount_price || false;
        result.discount_promotion = this.discount_promotion || null;
        result.price_with_discount = this.price_with_discount || false;
        result.price_without_discount = this.price_without_discount || false;
        result.promotion_id = this.promotion_id || false;
        result.promotion_price = this.promotion_price || false;
        result.original_price = this.original_price || false;
        result.available_promotion_pricelists = this.available_promotion_pricelists || false;
        return result;
    },

    getDisplayData() {
        const res = super.getDisplayData(...arguments);
        res.coupon_code = this.coupon_code;
        res.discount_price = this.discount_price;
        res.discount_promotion = this.discount_promotion;
        return res;
    },

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
        const posTaxId = this.pos.config.tax_id
            ? [this.pos.config.tax_id[0]]
            : null;

        const ptaxes_ids = posTaxId || this.tax_ids || this.get_product().taxes_id;
        const ptaxes_set = {};

        for (let i = 0; i < ptaxes_ids.length; i++) {
            ptaxes_set[ptaxes_ids[i]] = true;
        }

        const taxes = [];
        for (let i = 0; i < this.pos.taxes.length; i++) {
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

        const taxes_ids = posTaxId || this.tax_ids || this.get_product().taxes_id;
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

    setCoupon(data) {
        this.coupon_code = data.code;
        this.coupon_id = data.coupon_id;
        this.discount_price = data.discount_price;
        this.price_with_discount = data.price_with_discount;
        this.price_without_discount = data.price_without_discount;
    },

    removeCoupon() {
        const price_without_discount = this.price_without_discount;
        this.coupon_id = null;
        this.coupon_code = null;
        this.discount_price = null;
        this.price_with_discount = null;
        this.price_without_discount = null;
        this.set_unit_price(price_without_discount);
    },

    setPromotion(promo) {
        this.promotion_id = promo.promotion_id;
        this.promotion_price = promo.promotion_price;
        this.original_price = promo.original_price;
        this.available_promotion_pricelists = promo.available_promotion_pricelists;
        this.discount_promotion = promo.discount_promotion
    },

    removePromotion() {
        const original_price = this.original_price;
        this.promotion_id = null;
        this.promotion_price = null;
        this.original_price = null;
        this.available_promotion_pricelists = null;
        this.gross_original_price = null;
        this.discount_promotion = null;
        this.set_unit_price(original_price);
    },
});
