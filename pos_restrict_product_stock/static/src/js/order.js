/** @odoo-module */

import { Order } from "@point_of_sale/app/store/models";
import { Orderline } from "@point_of_sale/app/store/models";
import { ConfirmPopup } from "@point_of_sale/app/utils/confirm_popup/confirm_popup";
import { patch } from "@web/core/utils/patch";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";


patch(Order.prototype, {
    async pay() {
        if (!this.canPay()) {
            return;
        }
        const invalidQty = this.orderlines.some(line => line.quantity === 0);
        if (invalidQty) {
            await this.env.services.popup.add(ErrorPopup, {
                title: _t("Check quantities"),
                body: _t("Quantity needs to be greater than 0 for all products."),
            });
            return false;
        }
        const invalidPrice = this.orderlines.some(line => !line.coupon_id && line.get_unit_price() <= 0);
        if (invalidPrice) {
            await this.env.services.popup.add(ErrorPopup, {
                title: _t("Check prices"),
                body: _t("Check order lines: some items have a price of 0 or less."),
            });
            return false;
        }
        if (
            this.orderlines.some(
                (line) => line.get_product().tracking !== "none" && !line.has_valid_product_lot()
            ) &&
            (this.pos.picking_type.use_create_lots || this.pos.picking_type.use_existing_lots)
        ) {
            const { confirmed } = await this.env.services.popup.add(ConfirmPopup, {
                title: _t("Some Serial/Lot Numbers are missing"),
                body: _t(
                    "You are trying to sell products with serial/lot numbers, but some of them are not set.\nWould you like to proceed anyway?"
                ),
                confirmText: _t("Yes"),
                cancelText: _t("No"),
            });
            if (confirmed) {
                this.pos.mobile_pane = "right";
                this.env.services.pos.showScreen("PaymentScreen");
            }
        } else {
            this.pos.mobile_pane = "right";
            this.env.services.pos.showScreen("PaymentScreen");
        }
    },
    export_as_JSON() {
        const result = super.export_as_JSON(...arguments);
        if (!result) {
            return result;
        }
        return Object.assign(result, {
            reference: this.reference,
        });
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.reference = json.reference || false;
    },
    get_total_with_tax() {
        let total = 0;
        this.orderlines.forEach(line => {
            const line_total = line.get_price_with_tax();
            total += Math.round(line_total * 100) / 100;
        });
        return total;
    },
    async set_pricelist(pricelist) {
        var self = this;
        const invalidLines = [];
        const orderlines = this.get_orderlines();

        for (const line of orderlines) {
            if (line.promotion_price && line.available_promotion_pricelists && !line.available_promotion_pricelists.includes(pricelist.id)) {
                invalidLines.push(line);
            }
        }

        if (invalidLines.length) {
            const { confirmed } = await this.env.services.popup.add(ConfirmPopup, {
                title: _t("Invalid Promotions"),
                body: _t(
                    "Changing the price list will remove the applied promotions.\n\nDo you want to continue?"
                ),
            });
            if (!confirmed) {
                return;
            }
        }

        const linesWithCoupon = orderlines.filter(line => line.coupon_id);

        if (linesWithCoupon.length) {
            const { confirmed } = await this.env.services.popup.add(ConfirmPopup, {
                title: _t("Change Pricelist"),
                body: _t(
                    "At least one order line has an applied coupon. " +
                    "Changing the pricelist will remove all coupons and you will need to apply them again. " +
                    "Do you want to continue?"
                ),
                confirmText: _t("Yes, change pricelist"),
                cancelText: _t("Cancel"),
            });

            if (!confirmed) {
                return;
            }
        }
        this.pricelist = pricelist;

        for (const line of invalidLines) {
            line.removePromotion();
        }

        for (const line of linesWithCoupon) {
            line.removeCoupon();
        }

        const lines_to_recompute = orderlines.filter(
            (line) =>
                !(line.comboLines?.length || line.comboParent)
        );
        lines_to_recompute.forEach((line) => {
            line.set_unit_price(
                line.product.get_price(self.pricelist, line.get_quantity(), line.get_price_extra()), true
            );
            self.fix_tax_included_price(line);
        });
        const combo_parent_lines = orderlines.filter(
            (line) => line.comboLines?.length
        );
        const attributes_prices = {};
        combo_parent_lines.forEach((parentLine) => {
            attributes_prices[parentLine.id] = this.compute_child_lines(
                parentLine.product,
                parentLine.comboLines.map((childLine) => {
                    const comboLineCopy = { ...childLine.comboLine };
                    if (childLine.attribute_value_ids) {
                        comboLineCopy.configuration = {
                            attribute_value_ids: childLine.attribute_value_ids,
                        };
                    }
                    return comboLineCopy;
                }),
                pricelist
            );
        });
        const combo_children_lines = orderlines.filter(
            (line) => line.comboParent
        );
        combo_children_lines.forEach((line) => {
            line.set_unit_price(
                attributes_prices[line.comboParent.id].find(
                    (item) => item.comboLine.id === line.comboLine.id
                ).price, true
            );
            self.fix_tax_included_price(line);
        });
    },
    updatePricelistAndFiscalPosition(newPartner) {
        let newPartnerFiscalPosition;
        const defaultFiscalPosition = this.pos.fiscal_positions.find(
            (position) => position.id === this.pos.config.default_fiscal_position_id[0]
        );
        if (newPartner) {
            newPartnerFiscalPosition = newPartner.property_account_position_id
                ? this.pos.fiscal_positions.find(
                      (position) => position.id === newPartner.property_account_position_id[0]
                  )
                : defaultFiscalPosition;
        } else {
            newPartnerFiscalPosition = defaultFiscalPosition;
        }
        this.set_fiscal_position(newPartnerFiscalPosition);
    },
    async addCouponPerLine(code, line) {
        const result = await this.pos.env.services.orm.call(
            "loyalty.program",
            "pos_validate_coupon_per_line",
            [],
            {
                code: code,
                product_id: line.product.id,
                qty: line.get_quantity(),
                price_unit: line.get_unit_price(),
                config_id: this.pos.config.id,
                price_with_tax: line.get_price_with_tax(),
                pricelist_id: this.pricelist.id,
            }
        );    
        if (!result.valid) {
            await this.pos.env.services.popup.add(ErrorPopup, {
                title: _t("Coupon: %s", code),
                body: result.message,
            });
            return;
        }
        return result;
    },
    async add_product(product, options) {
        if (product.promotion_price) {
            options.price = product.promotion_price;
            options.extras.price_type = "manual";
            options.merge = true;
        }

        if (
            this.pos.doNotAllowRefundAndSales() &&
            this._isRefundOrder() &&
            (!options.quantity || options.quantity > 0)
        ) {
            this.pos.env.services.popup.add(ErrorPopup, {
                title: _t("Refund and Sales not allowed"),
                body: _t("It is not allowed to mix refunds and sales"),
            });
            return;
        }
        if (this._printed) {
            // when adding product with a barcode while being in receipt screen
            this.pos.removeOrder(this);
            return await this.pos.add_new_order().add_product(product, options);
        }
        this.assert_editable();
        options = options || {};
        const quantity = options.quantity ? options.quantity : 1;
        const line = new Orderline(
            { env: this.env },
            { pos: this.pos, order: this, product: product, quantity: quantity }
        );

        if (product.promotion_price) {
            const promo = this.pos.promotions.find(p => p.product_id === product.id);
            if (promo) {
                const promotion = {
                    promotion_id: promo.promotion_id,
                    promotion_price: promo.price,
                    original_price: promo.original_price,
                    available_promotion_pricelists: promo.pricelist_ids,
                    discount_promotion: product.original_price - promo.gross_price
                };
                line.setPromotion(promotion);
            }
        }

        this.fix_tax_included_price(line);

        this.set_orderline_options(line, options);
        line.set_full_product_name();

        if (line.is_lot_tracked()) {
            let related_lines = [];
            const price = line.product.get_price(
                line.order.pricelist,
                line.get_quantity(),
                line.get_price_extra(),
                false,
                line,
                related_lines,
            );
            related_lines.forEach((line) => line.set_unit_price(price));
        }
        var to_merge_orderline;
        for (var i = 0; i < this.orderlines.length; i++) {
            if (this.orderlines.at(i).can_be_merged_with(line) && options.merge !== false) {
                to_merge_orderline = this.orderlines.at(i);
            }
        }
        if (to_merge_orderline) {
            to_merge_orderline.merge(line);
            this.select_orderline(to_merge_orderline);
        } else {
            this.add_orderline(line);
            this.select_orderline(this.get_last_orderline());
        }

        if (options.draftPackLotLines) {
            this.selected_orderline.setPackLotLines({
                ...options.draftPackLotLines,
                setQuantity: options.quantity === undefined,
            });
        }

        if (options.comboLines?.length) {
            await this.addComboLines(line, options);
            // Make sure the combo parent is selected.
            this.select_orderline(line);
        }
        this.hasJustAddedProduct = true;
        clearTimeout(this.productReminderTimeout);
        this.productReminderTimeout = setTimeout(() => {
            this.hasJustAddedProduct = false;
        }, 3000);
        return line;
    }
});
