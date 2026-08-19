# -*- coding: utf-8 -*-
from odoo import models


class AccountMoveLine(models.Model):
    """CFDI local taxes (l10n_mx_local_tax + base 'gross') must be computed
    over the line amount BEFORE the discount. gross = net / (1 - discount/100),
    so we rescale the local tax result from the standard engine. Keeps the
    posted tax line and the invoice total in sync with the implocal node."""

    _inherit = "account.move.line"

    _XE_LOCAL_SCALED_KEYS = (
        "amount_currency", "balance", "tax_base_amount",
        "base_amount_currency", "amount", "base_amount",
    )

    def _compute_all_tax(self):
        super()._compute_all_tax()
        rl_model = self.env["account.tax.repartition.line"]
        for line in self:
            discount = line.discount or 0.0
            if line.display_type != "product" or not discount:
                continue
            factor = 1.0 - (discount / 100.0)
            if not factor:
                continue
            tax_map = line.compute_all_tax
            if not tax_map:
                continue
            new_map = {}
            changed = False
            for key, vals in tax_map.items():
                rep_id = key.get("tax_repartition_line_id") if hasattr(key, "get") else None
                tax = rl_model.browse(rep_id).tax_id if rep_id else False
                if tax and tax.l10n_mx_local_tax and tax.l10n_mx_local_base == "gross":
                    vals = dict(vals)
                    for fname in self._XE_LOCAL_SCALED_KEYS:
                        value = vals.get(fname)
                        if isinstance(value, (int, float)) and value:
                            vals[fname] = value / factor
                    changed = True
                new_map[key] = vals
            if changed:
                line.compute_all_tax = new_map
