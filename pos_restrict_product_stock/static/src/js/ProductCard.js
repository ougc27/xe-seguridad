/** @odoo-module **/
import { ProductCard } from "@point_of_sale/app/generic_components/product_card/product_card";

export class ProductCardExtended extends ProductCard {

    static props = {
        ...ProductCard.props,
        originalPrice: String,
    };

}