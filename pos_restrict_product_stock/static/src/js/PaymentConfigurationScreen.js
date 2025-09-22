/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { usePos } from "@point_of_sale/app/store/pos_hook";

export class PaymentConfiguratorScreen extends Component {

    setup() {
        this.orm = useService("orm");
        this.pos = usePos();
        this.state = useState({
            payment_plan: '',

        });
    }

    get currentOrder() {
        return this.pos.get_order();
    }

    async goToPayment() {
        const payment_plan = this.state.payment_plan;
        if (payment_plan) {
            const order = this.currentOrder;
            order.payment_plan = payment_plan;
            this.env.services.pos.mobile_pane = "right";
            this.env.services.pos.showScreen("PaymentScreen");
        }
    }

    goBack() {
        const order = this.currentOrder;
        if (!order.pos.config.tax_id) {
            this.env.services.pos.showScreen("ProductScreen");
        }
        const posTaxId = order.pos.config.tax_id
            ? [order.pos.config.tax_id[0]]
            : null; 
        const lines = order.get_orderlines();
    
        const payload = lines.map(line => {
            return {
                product_id: line.product.id,
                currency_id: order.pos.currency.id,
                pricelist_id: order.pricelist.id,
                quantity: line.quantity,
                taxes_id: posTaxId || line.tax_ids,
            };
        });
    
        this.orm.call(
            "product.pricelist",
            "get_product_pricelist_by_installment",
            [],
            {
                lines: payload,
                payment_plan: "cash",
            }
        ).then(newPrices => {
            lines.forEach((line, idx) => {
                if (newPrices[idx]) {
                    line.set_unit_price(newPrices[idx].price);
                }
            });
            this.env.services.pos.showScreen("ProductScreen");
        }).catch(error => {
            console.error("Error restoring prices on goBack:", error);
            this.env.services.pos.showScreen("ProductScreen");
        });
    }
    async selectPlan(ev) {
        this.state.payment_plan = ev.target.value;
        const order = this.currentOrder;
        const posTaxId = order.pos.config.tax_id
            ? [order.pos.config.tax_id[0]]
            : null; 
        const lines = order.get_orderlines();
        const payload = lines.map(line => {
            let pricelist_id = order.pricelist.id;
            if (line.product.original_price) {
                const promo = this.pos.promotions?.find(
                    p => p.product_id === line.product.id
                );
                pricelist_id = promo.pricelist_id;
            }
            return {
                product_id: line.product.id,
                currency_id: order.pos.currency.id,
                pricelist_id: pricelist_id,
                quantity: line.quantity,
                taxes_id: posTaxId || line.tax_ids,
            };
        });
        try {
            const newPrices = await this.orm.call(
                "product.pricelist",
                "get_product_pricelist_by_installment",
                [],
                {
                    lines: payload,
                    payment_plan: this.state.payment_plan,
                }
            );

            lines.forEach((line, idx) => {
                if (newPrices[idx]) {
                    line.set_unit_price(newPrices[idx].price);
                }
            });
        } catch (error) {
            console.error("Error recalculating prices:", error);
        }

    }
}

PaymentConfiguratorScreen.template = "PaymentConfiguratorScreen";
registry.category("pos_screens").add("PaymentConfiguratorScreen", PaymentConfiguratorScreen);
