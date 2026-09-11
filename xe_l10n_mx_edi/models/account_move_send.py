from odoo import models


class AccountMoveSend(models.TransientModel):

    _inherit = 'account.move.send'

    def _call_web_service_before_invoice_pdf_render(self, invoices_data):
        # l10n_mx_edi only calls _l10n_mx_edi_cfdi_invoice_try_send() when
        # l10n_mx_edi_is_cfdi_needed is True, so a blocked move (is_cfdi_needed already
        # False) never reaches that guard through this wizard - it's just silently
        # skipped. Raise here instead so checking the CFDI box for a blocked move
        # fails loudly, same as a direct RPC/server action call would.
        for invoice, invoice_data in invoices_data.items():
            if invoice_data.get('l10n_mx_edi_cfdi') and invoice._is_cfdi_issued_by_third_party():
                invoice._check_cfdi_not_issued_by_third_party()
        return super()._call_web_service_before_invoice_pdf_render(invoices_data)
