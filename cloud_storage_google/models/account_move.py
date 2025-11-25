# coding: utf-8

from odoo import models, _


class AccountMove(models.Model):
    _inherit = 'account.move'

    def button_request_cancel(self):
        for attachment in self.l10n_mx_edi_document_ids.attachment_id:
            attachment.sudo().download_file_from_gcs()
        return super(AccountMove, self).button_request_cancel()
