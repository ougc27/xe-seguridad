/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { Product } from "@point_of_sale/app/store/models";
import RestrictStockPopup from "@pos_restrict_product_stock/js/RestrictStockPopup"
import ProductDamagePopup from "@pos_restrict_product_stock/js/ProductDamagePopup"
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { _t } from "@web/core/l10n/translation";


patch(PosStore.prototype, {
    /*setup() {
        super.setup();
        this.all_pricelists = [];
    },*/
    async addProductToCurrentOrder(...args) {
        var type = this.config.stock_type
        const product_id = args['0'].id
        const [qty_available, virtual_available] = await this.getProductQuantity(product_id)
        const isDoor = args['0'].categ_id[1].includes("Puertas");
        if (this.config.is_restrict_product && ((type == 'qty_on_hand') && (qty_available <= 0)) | ((type == 'virtual_qty') && (virtual_available <= 0)) |
            ((args['0'].qty_available <= 0) && (args['0'].virtual_available <= 0))) {
            this.popup.add(RestrictStockPopup, {
                body: args['0'].display_name,
                pro_id: product_id
            });
        }    
        else{
            if (isDoor && this.company.id == 3) {
                const image_url = args['0'].getImageUrl();
                const posTaxId = this.config.tax_id
                    ? [this.config.tax_id[0]]
                    : args['0'].taxes_id;
                await this.popup.add(ProductDamagePopup, {
                    title: _t("Select Quantity and Type of Damage"),
                    body: args['0'].display_name,
                    image_url: image_url,
                    pro_id: product_id,
                    currency_id: this.currency.id,
                    pricelist_id: this.selectedOrder.pricelist.id,
                    taxes_id: posTaxId,
                    allow_outlet2: this.config.allow_outlet2,
                    qty_on_hand: qty_available
                });
            }
            else {
                await super.addProductToCurrentOrder(...args);
                const orderlines = this.env.services.pos.selectedOrder.orderlines;
                orderlines.forEach(line => {
                    if (line.product.id === product_id) {
                        line.qty_available = qty_available;
                        line.no_stock = false;
                    }
                });
            }
        }
    },
    async getProductQuantity(product_id) {
        const picking_type_id = this.env.services.pos.picking_type.id;
        return await this.orm.call("product.product", "get_product_quantity", [], {
            picking_id: picking_type_id,
            product_id: product_id
        });
    },
    async after_load_server_data() {
        await super.after_load_server_data(...arguments);
        for (const order of this.get_order_list()) {
            const line = order.get_last_orderline();
            if (line && !line.damage_type && line.product.categ.name.toLowerCase() === "puertas") {
                const damage_type = await this.orm.call(
                    "product.pricelist",
                    "get_product_damage_type",
                    [],
                    {
                        product_id: line.product.id,
                        currency_id: line.order.pos.currency.id,
                        quantity: line.quantity,
                        price: line.price,
                    }
                );
                line.damage_type = damage_type;
            }
        }
    },
    async _processData(loadedData) {
        this.all_pricelists = loadedData["all_pricelists"];
        await super._processData(loadedData);
    },
    _loadProductProduct(products) {
        const productMap = {};
        const productTemplateMap = {};

        const modelProducts = products.map((product) => {
            product.pos = this;
            product.env = this.env;
            product.applicablePricelistItems = {};
            productMap[product.id] = product;
            productTemplateMap[product.product_tmpl_id[0]] = (
                productTemplateMap[product.product_tmpl_id[0]] || []
            ).concat(product);
            return new Product(product);
        });
        const pricelists = this.all_pricelists || this.pricelists;
        for (const pricelist of pricelists) {
            for (const pricelistItem of pricelist.items) {
                if (pricelistItem.product_id) {
                    const product_id = pricelistItem.product_id[0];
                    const correspondingProduct = productMap[product_id];
                    if (correspondingProduct) {
                        this._assignApplicableItems(pricelist, correspondingProduct, pricelistItem);
                    }
                } else if (pricelistItem.product_tmpl_id) {
                    const product_tmpl_id = pricelistItem.product_tmpl_id[0];
                    const correspondingProducts = productTemplateMap[product_tmpl_id];
                    for (const correspondingProduct of correspondingProducts || []) {
                        this._assignApplicableItems(pricelist, correspondingProduct, pricelistItem);
                    }
                } else {
                    for (const correspondingProduct of products) {
                        this._assignApplicableItems(pricelist, correspondingProduct, pricelistItem);
                    }
                }
            }
        }
        this.db.add_products(modelProducts);
    }
});
