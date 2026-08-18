from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    pos_price_include = fields.Boolean(
        string="Tax Included in POS Price",
        help="Point of Sale flag: when the tax is used inside a POS context "
             "(order, closing entry and the invoice generated from the POS), the "
             "price is treated as tax-included and the tax is computed backwards, "
             "even though 'Included in Price' stays off for every other channel. "
             "This lets the same tax be tax-excluded on regular sales and "
             "tax-included only in the Point of Sale, without duplicating the tax.",
    )

    def compute_all(self, price_unit, currency=None, quantity=1.0, product=None,
                    partner=None, is_refund=False, handle_price_include=True,
                    include_caba_tags=False, fixed_multiplicator=1):
        """POS 'tax included' mode.

        When the computation runs in a POS context (context flag
        'pos_price_include_mode') and every tax being computed is flagged
        'pos_price_include', we turn on the NATIVE 'force_price_include'
        context. Odoo's compute_all honours that flag everywhere, so the tax
        is computed backwards (base = price / (1 + rate)) exactly like a real
        price-included tax, but WITHOUT touching the stored 'price_include'
        used by the other channels and WITHOUT creating a second tax record.
        """
        if (
            self
            and self.env.context.get('pos_price_include_mode')
            and not self.env.context.get('force_price_include')
            and all(tax.pos_price_include for tax in self)
        ):
            self = self.with_context(force_price_include=True)
        return super().compute_all(
            price_unit, currency=currency, quantity=quantity, product=product,
            partner=partner, is_refund=is_refund,
            handle_price_include=handle_price_include,
            include_caba_tags=include_caba_tags,
            fixed_multiplicator=fixed_multiplicator,
        )
