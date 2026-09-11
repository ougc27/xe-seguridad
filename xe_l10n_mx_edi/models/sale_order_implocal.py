# -*- coding: utf-8 -*-
from odoo import models
from odoo.tools.misc import formatLang


class SaleOrder(models.Model):
    """Adjust the quotation tax summary (tax_totals) so local gross-based taxes
    (e.g. the 5-al-millar vigilancia) are shown over the pre-discount base,
    matching the posted invoice. Purely cosmetic: it only edits the displayed
    totals widget, never the invoice, accounting or CFDI. Any structural
    mismatch falls back to the native value without breaking the view."""

    _inherit = "sale.order"

    def _compute_tax_totals(self):
        super()._compute_tax_totals()
        for order in self:
            try:
                order._xe_adjust_local_gross_tax_totals()
            except Exception:
                # Never break the totals widget over a cosmetic adjustment.
                pass

    def _xe_adjust_local_gross_tax_totals(self):
        self.ensure_one()
        totals = self.tax_totals
        if not isinstance(totals, dict):
            return

        # Sum the gross vs net delta per tax group.
        deltas = {}
        for line in self.order_line:
            delta = line._xe_local_gross_tax_delta()
            if not delta:
                continue
            group = line.tax_id.filtered(
                lambda t: t.l10n_mx_local_tax and t.l10n_mx_local_base == "gross"
            ).tax_group_id[:1]
            if not group:
                continue
            deltas[group.id] = deltas.get(group.id, 0.0) + delta

        if not deltas:
            return

        currency = self.currency_id or self.company_id.currency_id
        total_delta = sum(deltas.values())

        # Adjust each affected tax group amount in the breakdown.
        for groups in (totals.get("groups_by_subtotal") or {}).values():
            for grp in groups:
                gid = grp.get("tax_group_id")
                if gid in deltas:
                    grp["tax_group_amount"] = grp.get("tax_group_amount", 0.0) + deltas[gid]
                    grp["formatted_tax_group_amount"] = formatLang(
                        self.env, grp["tax_group_amount"], currency_obj=currency
                    )

        # Adjust the grand total.
        if "amount_total" in totals:
            totals["amount_total"] = totals.get("amount_total", 0.0) + total_delta
            totals["formatted_amount_total"] = formatLang(
                self.env, totals["amount_total"], currency_obj=currency
            )

        self.tax_totals = totals
