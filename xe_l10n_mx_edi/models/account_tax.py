# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountTax(models.Model):
    _inherit = "account.tax"

    # --- CFDI "ImpuestosLocales" (implocal) support -------------------------
    # A "local" tax (e.g. the 5-al-millar "vigilancia" retention charged by
    # some Mexican states/municipalities) must NOT be reported in the federal
    # <cfdi:Impuestos> node. Instead it goes into the
    # <implocal:ImpuestosLocales> complement. These flags drive that behaviour.

    l10n_mx_local_tax = fields.Boolean(
        string="Local Tax (ImpuestosLocales)",
        help="If enabled, this tax is reported inside the CFDI "
        "'implocal:ImpuestosLocales' complement instead of the federal "
        "'cfdi:Impuestos' node. The label printed as 'ImpLocRetenido' / "
        "'ImpLocTrasladado' is taken from the tax 'Label on Invoices' "
        "(invoice_label) field.",
    )
    l10n_mx_local_type = fields.Selection(
        selection=[
            ("retencion", "Local retention (RetencionesLocales)"),
            ("traslado", "Local transfer (TrasladosLocales)"),
        ],
        string="Local Tax Type",
        default="retencion",
    )
    l10n_mx_local_base = fields.Selection(
        selection=[
            ("gross", "Subtotal before discount (gross)"),
            ("net", "Subtotal after discount (net)"),
        ],
        string="Local Tax Base",
        default="gross",
        help="Base used to compute the amount reported in the complement.\n"
        "- gross: the line amount BEFORE any line discount (e.g. anticipo "
        "amortization). This matches the '5 al millar' rule where the "
        "retention is computed over the full estimated work value.\n"
        "- net: the line amount after discount (standard Odoo behaviour).",
    )
