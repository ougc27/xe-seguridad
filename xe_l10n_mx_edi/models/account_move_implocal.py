# -*- coding: utf-8 -*-
from odoo import models
from odoo.tools.float_utils import float_round


class AccountMoveImplocal(models.Model):
    """CFDI 4.0 'implocal:ImpuestosLocales' complement.

    NOTE: kept in a dedicated file so we do NOT touch the existing
    account_move.py of this module. Odoo merges every '_inherit' class for the
    same model, so this simply adds behaviour on top.
    """

    _inherit = "account.move"

    # ------------------------------------------------------------------ #
    # Hook: _l10n_mx_edi_add_invoice_cfdi_values already exists on
    # account.move in the installed l10n_mx_edi (it is called from this
    # module's own payment flow, see account_move.py ->
    # _l10n_mx_edi_add_payment_cfdi_values). By the time super() returns,
    # cfdi_values is fully populated by
    # l10n_mx_edi.document._add_base_lines_cfdi_values (subtotal, descuento,
    # total, traslados_list, retenciones_reduced_list, conceptos_list, ...).
    # We post-process it to move local taxes into the implocal complement.
    # ------------------------------------------------------------------ #
    def _l10n_mx_edi_add_invoice_cfdi_values(self, cfdi_values):
        res = super()._l10n_mx_edi_add_invoice_cfdi_values(cfdi_values)
        self._xe_add_implocal_cfdi_values(cfdi_values)
        return res

    def _xe_get_local_tax_lines(self):
        """Tax lines of this move whose tax is flagged as a CFDI local tax."""
        return self.line_ids.filtered(
            lambda l: l.tax_line_id and l.tax_line_id.l10n_mx_local_tax
        )

    def _xe_local_tax_base(self, tax):
        """Base for a local tax on this invoice.

        gross -> sum of price_unit * quantity (BEFORE the line discount),
                 i.e. the '5 al millar' rule over the full work value.
        net   -> standard post-discount subtotal.
        """
        base_lines = self.invoice_line_ids.filtered(
            lambda l: tax in l.tax_ids and l.display_type == "product"
        )
        if tax.l10n_mx_local_base == "gross":
            return sum(line.quantity * line.price_unit for line in base_lines)
        return sum(base_lines.mapped("price_subtotal"))

    def _xe_add_implocal_cfdi_values(self, cfdi_values):
        local_tax_lines = self._xe_get_local_tax_lines()
        if not local_tax_lines:
            return

        currency = cfdi_values.get("currency") or self.currency_id

        retenciones, traslados = [], []
        total_ret = 0.0
        total_tras = 0.0
        for tax in local_tax_lines.tax_line_id:
            base = self._xe_local_tax_base(tax)
            importe = currency.round(base * (abs(tax.amount) / 100.0))
            entry = {
                "impuesto": (tax.invoice_label or tax.name or "")[:100],
                # implocal expects the rate as a percentage with 2 decimals,
                # e.g. 0.5% -> "0.50".
                "tasa": float_round(abs(tax.amount), precision_digits=2),
                "importe": importe,
            }
            if tax.l10n_mx_local_type == "traslado":
                traslados.append(entry)
                total_tras += importe
            else:
                retenciones.append(entry)
                total_ret += importe

        cfdi_values["implocales"] = {
            "version": "1.0",
            "total_retenciones": currency.round(total_ret),
            "total_traslados": currency.round(total_tras),
            "retenciones_list": retenciones,
            "traslados_list": traslados,
        }

        # Keep the Comprobante 'Total' consistent with the local taxes: local
        # retentions reduce the total, local transfers increase it. (The base
        # builder does not know about local taxes.)
        if cfdi_values.get("total") is not None:
            cfdi_values["total"] = currency.round(
                cfdi_values["total"] - total_ret + total_tras
            )

        # Defensive: never duplicate a local tax inside the federal node.
        # Local taxes carry no SAT impuesto code (001/002/003), so normally
        # they never reach these lists; this is a safety net only.
        local_labels = {
            (t.invoice_label or t.name or "")[:100]
            for t in local_tax_lines.tax_line_id
        }

        def _is_local(entry):
            return entry.get("impuesto") in local_labels

        for key in ("retenciones_reduced_list", "traslados_list"):
            if cfdi_values.get(key):
                cfdi_values[key] = [e for e in cfdi_values[key] if not _is_local(e)]
        for concepto in cfdi_values.get("conceptos_list", []):
            for key in ("retenciones_list", "traslados_list"):
                if concepto.get(key):
                    concepto[key] = [e for e in concepto[key] if not _is_local(e)]
