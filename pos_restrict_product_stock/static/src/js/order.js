/** @odoo-module */

import { Order } from "@point_of_sale/app/store/models";
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
        const orderlines = this.get_orderlines();
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
    }
});
