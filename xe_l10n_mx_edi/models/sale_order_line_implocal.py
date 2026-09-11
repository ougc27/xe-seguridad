# -*- coding: utf-8 -*-
from odoo import models


class SaleOrderLine(models.Model):
    """Cosmetic mirror of the invoice fix on the quotation.

    Makes CFDI local taxes with base 'gross' show on the sale order computed
    over the pre-discount amount (like the posted invoice), so the quotation
    summary / PDF matches the invoice. This ONLY affects lines that carry a
    local gross-based tax AND have a discount; every other line is untouched.
    It does not change how the invoice is generated (that already works).
    """

    _inherit = "sale.order.line"

    def _xe_local_gross_tax_delta(self):
        """gross_local_tax - net_local_tax for this line (0 if not applicable).

        For a retention this is negative (the gross retention is larger), so it
        is added on top of the standard net-based result.
        """
        self.ensure_one()
        discount = self.discount or 0.0
        if self.display_type or not discount:
            return 0.0
        local = self.tax_id.filtered(
            lambda t: t.l10n_mx_local_tax and t.l10n_mx_local_base == "gross"
        )
        if not local:
            return 0.0
        factor = 1.0 - discount / 100.0
        if not factor:
            return 0.0

        currency = self.order_id.currency_id or self.company_id.currency_id
        partner = self.order_id.partner_shipping_id or self.order_partner_id
        kwargs = {"product": self.product_id, "partner": partner}

        net = local.compute_all(
            self.price_unit * factor, currency, self.product_uom_qty, **kwargs
        )
        gross = local.compute_all(
            self.price_unit, currency, self.product_uom_qty, **kwargs
        )
        delta = sum(t["amount"] for t in gross["taxes"]) - sum(
            t["amount"] for t in net["taxes"]
        )
        return currency.round(delta) if currency else delta

    def _compute_amounts(self):
        super()._compute_amounts()
        for line in self:
            delta = line._xe_local_gross_tax_delta()
            if delta:
                line.price_tax += delta
                line.price_total += delta
