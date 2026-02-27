from odoo import api, models, fields, _
from odoo.osv import expression
from odoo.tools.safe_eval import safe_eval


class LoyaltyProgram(models.Model):
    _inherit = "loyalty.program"

    allow_multiple_use = fields.Boolean(
        string="Allow Multiple Uses",
        help="If enabled, coupons from this program can be used multiple times."
    )

    externally_managed = fields.Boolean(
        string="Externally Managed Program",
        help="If enabled, coupons for this program are managed externally and "
            "cannot be generated or edited manually."
    )

    @api.model
    def pos_validate_coupon_per_line(
        self,
        code=None,
        product_id=None,
        qty=1.0,
        price_unit=0.0,
        config_id=None,
        price_with_tax=0.0,
        pricelist_id=None,
        **kwargs
    ):
        if not code or not product_id or not config_id:
            return {"valid": False, "message": _("Missing required data.")}

        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            return {"valid": False, "message": _("Invalid product.")}

        pos_config = self.env["pos.config"].sudo().browse(config_id).exists()
        if not pos_config:
            return {"valid": False, "message": _("Invalid POS configuration.")}

        qty = float(qty)
        price_unit = float(price_unit)
        line_subtotal = qty * price_unit

        if line_subtotal <= 0:
            return {"valid": False, "message": _("Invalid line amount.")}

        card = self.env["loyalty.card"].sudo().search(
            [
                ("code", "=", code),
                ("program_id", "in", pos_config._get_program_ids().ids),

                '|',
                ('product_id', '=', product.id),
                ('product_id', '=', False),

                '|',
                ('warehouse_id', '=', pos_config.picking_type_id.warehouse_id.id),
                ('warehouse_id', '=', False),

                '|',
                ('expiration_date', '=', False),
                ('expiration_date', '>=', fields.Date.context_today(self)),
            ],
            order="partner_id, points desc",
            limit=1,
        )

        if not card:
            return {"valid": False, "message": _("Invalid coupon code.")}

        program = card.program_id

        if card.source_pos_order_id and not program.allow_multiple_use:
            return {
                "valid": False,
                "message": _(
                    "This coupon has already been used in another order."
                )
            }

        # ---------------------------------------------------------
        # 2️⃣ Validar programa
        # ---------------------------------------------------------
        if not program.active:
            return {"valid": False, "message": _("Coupon program inactive.")}

        if program.pricelist_ids:
            if not pricelist_id or pricelist_id not in program.pricelist_ids.ids:
                pricelist_names = ", ".join(program.pricelist_ids.mapped("name"))

                return {
                    "valid": False,
                    "message": _(
                        "Coupon not applicable for the current price list.\n"
                        "Applicable price lists: %(lists)s",
                        lists=pricelist_names,
                    ),
                }

        if program.rule_ids:
            failure_reason = None
            rule_applies = False

            for rule in program.rule_ids:
                if rule.product_ids and product.id not in rule.product_ids.ids:
                    product_names = ", ".join(rule.product_ids.mapped("name"))
                    failure_reason = _("Coupon not applicable to this product.")
                    continue

                if (
                    rule.product_category_id
                    and (not product.categ_id or product.categ_id != rule.product_category_id)
                ):
                    failure_reason = _("Product category (%(categ_name)s) does not meet coupon conditions.",
                        categ_name = rule.product_category_id.name)
                    continue

                if (
                    rule.product_tag_id
                    and rule.product_tag_id not in product.product_tag_ids
                ):
                    failure_reason = _("Product tag (%(tag_name)s) does not meet coupon conditions.",
                        tag_name=rule.product_tag_id.name)
                    continue

                if rule.minimum_qty and qty < rule.minimum_qty:
                    failure_reason = _(
                        "Minimum quantity required: %(qty)s",
                        qty=rule.minimum_qty,
                    )
                    continue
            
                if rule.maximum_qty and qty > rule.maximum_qty:
                    failure_reason = _(
                        "Maximum quantity allowed: %(qty)s",
                        qty=rule.maximum_qty,
                    )
                    continue

            
                if rule.minimum_amount:
                    if rule.minimum_amount_tax_mode == "incl":
                        amount = price_with_tax
                        tax_label = _("with taxes included")
                    else:
                        amount = qty * price_unit
                        tax_label = _("without taxes")

                    if amount < rule.minimum_amount:
                        failure_reason = _(
                            "Minimum purchase amount %(tax_mode)s is %(required)s. "
                            "Current amount: %(current)s.",
                            tax_mode=tax_label,
                            required=rule.minimum_amount,
                            current=amount,
                        )
                        continue

                if rule.product_domain:
                    domain = [('id', '=', product.id)] + safe_eval(rule.product_domain)
                    if not product.search_count(domain):
                        failure_reason = _("Product does not match coupon product rules.")
                        continue

                rule_applies = True
                break

            if not rule_applies:
                return {
                    "valid": False,
                    "message": failure_reason or _(
                        "Coupon conditions not met for this product."
                    ),
            }

        rewards = program.reward_ids.filtered(
            lambda r:
            r.reward_type == "discount"
            #and not r.is_global_discount
            and r.discount_mode in ("percent", "amount", "per_order")
        )

        if not rewards:
            return {
                "valid": False,
                "message": _("No compatible discount reward found."),
            }

        reward = rewards[0]

        if reward.discount_applicability not in ("order", "specific_products"):
            return {
                "valid": False,
                "message": _("Unsupported discount applicability."),
            }

        def product_matches_reward(r):
            if r.discount_product_ids and product not in r.discount_product_ids:
                return False
            if r.discount_product_category_id and product.categ_id != r.discount_product_category_id:
                return False
            if r.discount_product_tag_id and r.discount_product_tag_id not in product.tag_ids:
                return False
            if r.discount_product_domain:
                domain = expression.normalize_domain(
                    eval(r.discount_product_domain)
                )
                if not product.filtered_domain(domain):
                    return False
            return True

        if reward.discount_applicability == "specific_products":
            if not product_matches_reward(reward):
                return {
                    "valid": False,
                    "message": _("Coupon not applicable to this product."),
                }

        discount_amount_tax = 0
        tax_ratio = line_subtotal / price_with_tax
        if card.price_from_pricelist:
            price_with_discount = card.price_from_pricelist
            new_line_subtotal = qty * price_with_discount
            discount_net = line_subtotal - new_line_subtotal
            tax_ratio = price_with_tax / line_subtotal if line_subtotal else 1
            discount_amount_tax = discount_net * tax_ratio
        else:
            if reward.discount_mode == "percent":
                discount_amount_tax = price_with_tax * (reward.discount / 100.0)

            else:  # amount | per_order
                discount_amount_tax = reward.discount

            max_discount_tax = reward.discount_max_amount

            if max_discount_tax:
                if reward.discount_application == "unit":
                    max_discount_tax = reward.discount_max_amount * qty
                discount_amount_tax = min(discount_amount_tax, max_discount_tax)

            else:
                if reward.discount_application == "unit":
                    discount_amount_tax = discount_amount_tax * qty

            if discount_amount_tax <= 0:
                return {
                    "valid": False,
                    "message": _("Discount results in zero amount."),
                }

            discount_amount_net = discount_amount_tax * tax_ratio

            if reward.discount_application == "unit":
                discount_per_unit = discount_amount_net / qty
                price_with_discount = price_unit - discount_per_unit

            elif reward.discount_application == "line":
                discounted_line_total = line_subtotal - discount_amount_net
                price_with_discount = discounted_line_total / qty

            else:
                return {
                    "valid": False,
                    "message": _("Invalid discount application configuration."),
                }
    
        return {
            "valid": True,
            "coupon_id": card.id,
            "program_id": program.id,
            "reward_id": reward.id,
            "code": code,
            "price_with_discount": price_with_discount,
            "price_without_discount": price_unit,
            "discount_price": discount_amount_tax,
        }

    @api.model
    def create_from_template(self, template_id):
        '''
        Creates the program from the template id defined in `get_program_templates`.

        Returns an action leading to that new record.
        '''
        template_values = self._get_template_values()
        if template_id not in template_values:
            return False
        program = self.create(template_values[template_id])
        action = {}
        if self.env.context.get('menu_type') == 'gift_ewallet':
            action = self.env['ir.actions.act_window']._for_xml_id('loyalty.loyalty_program_gift_ewallet_action')
            action['views'] = [[False, 'form']]
        else:
            action = self.env['ir.actions.act_window']._for_xml_id('loyalty.loyalty_program_discount_loyalty_action')
            view_id = self.env.ref('loyalty.loyalty_program_view_form').id
            action['views'] = [[view_id, 'form']]
        action['view_mode'] = 'form'
        action['res_id'] = program.id
        return action
