from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    meli_invoice_document_id = fields.Many2one(
        'meli.invoice.document', string='Mercado Libre Invoice Document',
        readonly=True, copy=False,
        help="Which Mercado Libre document (meli.invoice.document) is "
             "currently related to this invoice/credit note. Mercado "
             "Libre is the only source of the CFDI for this move — Odoo "
             "never stamps it through its own PAC (blocked by "
             "res.partner.cfdi_issued_by_third_party, see "
             "xe_l10n_mx_edi). Used to detect refacturación: when a "
             "newer meli.invoice.document exists for the same order "
             "with a different id than this field points to, the "
             "invoice gets cancelled and replaced.",
    )
