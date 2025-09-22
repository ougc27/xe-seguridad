import re
from odoo import models, api, _
from odoo.exceptions import UserError


class Pricelist(models.Model):
    _inherit = "product.pricelist"

    def apply_tax(self, currency_id, price, taxes_id, quantity):
        """Apply taxes to the given price using compute_all"""
        taxes = self.env["account.tax"].browse(taxes_id)
        if not taxes.exists():
            return {
                "price_excluded": price,
                "price_included": price,
            }
        res = taxes.compute_all(price, currency=self.env["res.currency"].browse(currency_id))
        return {
            "price_excluded": res["total_excluded"],
            "price_included": res["total_included"],
        }

    @api.model
    def get_product_pricelist(self, product_id, currency_id, pricelist_id, quantity, taxes_id, damage_type, promotion_price=0):
        product = self.env["product.product"].sudo().browse(product_id)

        if not product.exists():
            raise UserError(_("The product does not exist."))

        if promotion_price:
            return self.apply_tax(currency_id, promotion_price, taxes_id, quantity)

        if damage_type == "no_outlet":
            pricelist = self.env["product.pricelist"].sudo().browse(pricelist_id)
            if not pricelist.exists():
                raise UserError(_("The pricelist does not exist."))
            price = pricelist._compute_price_rule(product, quantity, None)[product_id][0]
            if not price:
                price = product.list_price
            return self.apply_tax(currency_id, price, taxes_id, quantity)

        if damage_type in ["outlet_1", "outlet_2"]:
            target = damage_type.replace("_", " ")

            pricelist = self.env["product.pricelist"].search([
                ("currency_id", "=", currency_id),
                ("name", "ilike", f"%{target}%"),
            ], limit=1)

            if not pricelist:
                raise UserError(_(f"No pricelist was found for {target}."))

            price = pricelist._compute_price_rule(product, quantity, None)[product_id][0]
            if not price:
                price = product.list_price

            return self.apply_tax(currency_id, price, taxes_id, quantity)

    @api.model
    def get_product_damage_type(self, product_id, currency_id, quantity, price):
        damage_type = "no_outlet"
        product = self.env["product.product"].sudo().browse(product_id)
        for outlet_name in ["outlet 1", "outlet 2"]:
            pricelist = self.env["product.pricelist"].search([
                ("currency_id", "=", currency_id),
                ("name", "ilike", f"%{outlet_name}%"),
            ], limit=1)

            if pricelist:
                line_price = pricelist._compute_price_rule(product, 1, None)[product_id][0]
                if abs(line_price - price) < 1:
                    damage_type = outlet_name.replace(" ", "_")
                    break

        return damage_type

    @api.model
    def get_product_pricelist_by_installment(self, lines, payment_plan):
        decrement = 0
        if payment_plan == "installment_6":
            decrement = 1
        elif payment_plan == "installment_12":
            decrement = 2

        result = []

        for line in lines:     
            pricelist = self.browse(line["pricelist_id"])       
            pv_number = None
            price = 0
            if pricelist and pricelist.name and pricelist.name.upper().startswith("PV"):
                match = re.search(r"^PV(\d+)", pricelist.name.upper())
                if match:
                    pv_number = int(match.group(1))

            new_pricelist = pricelist
            if pv_number and decrement > 0:
                target_number = pv_number - decrement
                new_pricelist = self.search(
                    [("name", "ilike", f"PV{target_number}")],
                    limit=1
                ) or pricelist

            product = self.env["product.product"].browse(line["product_id"])
            qty = line.get("quantity", 1.0)
            partner = self.env.user.partner_id 

            try:
                price = new_pricelist._get_product_price(product, qty, partner)
            except:
                price = product.list_price

            result.append({
                "product_id": product.id,
                "price": price,
            })

        return result
