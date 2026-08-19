# -*- coding: utf-8 -*-
import re
from markupsafe import Markup

from odoo import api, models

IMPLOCAL_NS = "http://www.sat.gob.mx/implocal"
IMPLOCAL_XSD = "http://www.sat.gob.mx/sitio_internet/cfd/implocal/implocal.xsd"

# SAT federal impuesto codes by l10n_mx_tax_type.
_SAT_TAX_CODE = {"isr": "001", "iva": "002", "ieps": "003"}


class L10nMxEdiDocument(models.Model):
    """CFDI 'implocal' complement plumbing.

    1) Keep CFDI local taxes OUT of the federal <cfdi:Impuestos> node.
       The SAT 'Tipo de impuesto' field (l10n_mx_tax_type) is required and only
       accepts isr/iva/ieps, so a local tax is unavoidably tagged as one of
       those and Odoo would report it federally (duplicating the retention that
       already lives in the implocal complement). We strip those entries from
       each base line BEFORE the federal node is built.

    2) Declare the 'implocal' namespace at the <cfdi:Comprobante> ROOT so the
       PAC does not reject the CFDI with error CO1002.
    """

    _inherit = "l10n_mx_edi.document"

    # ------------------------------------------------------------------ #
    # 1) Exclude local taxes from the federal node                       #
    # ------------------------------------------------------------------ #
    @api.model
    def _add_base_lines_cfdi_values(self, cfdi_values, base_lines, percentage_paid=None):
        self._xe_strip_local_taxes_from_base_lines(base_lines)
        return super()._add_base_lines_cfdi_values(
            cfdi_values, base_lines, percentage_paid=percentage_paid
        )

    @api.model
    def _xe_strip_local_taxes_from_base_lines(self, base_lines):
        for line in base_lines:
            record = line.get("record")
            taxes = record.tax_ids if record else self.env["account.tax"]
            local_taxes = taxes.filtered("l10n_mx_local_tax")
            if not local_taxes:
                continue

            # (impuesto_code, rate) footprint of the local taxes on this line.
            local_keys = {
                (
                    _SAT_TAX_CODE.get(tax.l10n_mx_tax_type),
                    round(abs(tax.amount) / 100.0, 6),
                )
                for tax in local_taxes
            }

            for list_key in ("withholding_values_list", "transferred_values_list"):
                values = line.get(list_key)
                if not values:
                    continue
                line[list_key] = [
                    tv
                    for tv in values
                    if (tv.get("impuesto"), round(tv.get("tasa_o_cuota") or 0.0, 6))
                    not in local_keys
                ]

    # ------------------------------------------------------------------ #
    # 2) Declare the implocal namespace at the Comprobante root          #
    # ------------------------------------------------------------------ #
    @api.model
    def _decode_cfdi_attachment(self, cfdi_data):
        return super()._decode_cfdi_attachment(
            self._xe_add_implocal_root_ns(cfdi_data)
        )

    @api.model
    def _xe_add_implocal_root_ns(self, cfdi_data):
        is_bytes = isinstance(cfdi_data, (bytes, bytearray))
        try:
            text = cfdi_data.decode("utf-8") if is_bytes else str(cfdi_data)
        except Exception:
            return cfdi_data

        if "implocal:ImpuestosLocales" not in text:
            return cfdi_data

        match = re.search(r"<cfdi:Comprobante\b[^>]*>", text)
        if not match:
            return cfdi_data

        tag = match.group(0)
        new_tag = tag

        if "xmlns:implocal=" not in new_tag:
            new_tag = new_tag.replace(
                "<cfdi:Comprobante",
                '<cfdi:Comprobante xmlns:implocal="%s"' % IMPLOCAL_NS,
                1,
            )

        sl = re.search(r'xsi:schemaLocation="([^"]*)"', new_tag)
        if sl and IMPLOCAL_NS not in sl.group(1):
            new_val = "%s %s %s" % (sl.group(1).rstrip(), IMPLOCAL_NS, IMPLOCAL_XSD)
            new_tag = new_tag.replace(
                sl.group(0), 'xsi:schemaLocation="%s"' % new_val, 1
            )

        if new_tag == tag:
            return cfdi_data

        text = text.replace(tag, new_tag, 1)

        if is_bytes:
            return text.encode("utf-8")
        if isinstance(cfdi_data, Markup):
            return Markup(text)
        return text
