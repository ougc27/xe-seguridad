from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'
    _description = "Account Move Send"

    @api.model
    def _call_web_service_before_invoice_pdf_render(self, invoices_data):
        # EXTENDS 'account'
        super()._call_web_service_before_invoice_pdf_render(invoices_data)

        for invoice, invoice_data in invoices_data.items():

            if invoice_data.get('l10n_mx_edi_cfdi') and self._get_default_l10n_mx_edi_enable_cfdi(invoice):
                # Sign it.
                invoice._l10n_mx_edi_cfdi_invoice_try_send()

                # Check for success.
                if invoice.l10n_mx_edi_cfdi_state == 'sent':
                    continue

                # Check for error.
                errors = []
                for document in invoice.l10n_mx_edi_invoice_document_ids:
                    if document.state == 'invoice_sent_failed':
                        errors.append(document.message)
                        break

                invoice_data['error'] = {
                    'error_title': _("Error when sending the CFDI to the PAC:"),
                    'errors': errors,
                }

                raise UserError(_("The invoice could not be validated: %s") % (', '.join(errors)))
