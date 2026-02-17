from odoo import fields, models, api
from .l10n_mx_edi_document import USAGE_SELECTION


class AccountMove(models.Model):
    
    _inherit = 'account.move'

    l10n_mx_edi_usage = fields.Selection(
        selection=USAGE_SELECTION,
        string="Usage",
        readonly=False,
        store=True,
        compute='_compute_l10n_mx_edi_usage',
        default="G03",
        tracking=True,
        help="Used in CFDI to express the key to the usage that will gives the receiver to this invoice. This "
             "value is defined by the customer.\nNote: It is not cause for cancellation if the key set is not the usage "
             "that will give the receiver of the document.",
    )

    def l10n_mx_edi_cfdi_invoice_try_update_payments(self):
        """ Try to update the state of payments for the current invoices. """
        warehouse_id = self.x_studio_almacen_id
        payments_diff = self._l10n_mx_edi_cfdi_invoice_get_payments_diff()

        for invoice, commands in payments_diff['to_remove'].items():
            invoice.l10n_mx_edi_invoice_document_ids = commands

        for payment, pay_results in payments_diff['to_process']:
            if not payment.x_studio_almacen_id:
                payment.write({
                    'warehouse_id': warehouse_id
                })
            payment.l10n_mx_edi_cfdi_invoice_try_update_payment(pay_results)

    @api.depends('l10n_mx_edi_cfdi_attachment_id')
    def _compute_l10n_mx_edi_cfdi_uuid(self):
        '''Fill the invoice fields from the cfdi values.
        '''
        for move in self:
            if move.l10n_mx_edi_cfdi_attachment_id:
                if not move.l10n_mx_edi_cfdi_attachment_id.datas:
                    move.l10n_mx_edi_cfdi_attachment_id.download_file_from_gcs()
                cfdi_infos = self.env['l10n_mx_edi.document']._decode_cfdi_attachment(move.l10n_mx_edi_cfdi_attachment_id.raw)
                move.l10n_mx_edi_cfdi_uuid = cfdi_infos.get('uuid')
            else:
                move.l10n_mx_edi_cfdi_uuid = None

    def l10n_mx_edi_cfdi_invoice_try_update_payment(self, pay_results, force_cfdi=False):
        """ Update the CFDI state of the current payment.

        :param pay_results: The amounts to consider for each invoice.
                            See '_l10n_mx_edi_cfdi_payment_get_reconciled_invoice_values'.
        :param force_cfdi:  Force the sending of the CFDI if the payment is PUE.
        """
        self.ensure_one()

        last_document = self.l10n_mx_edi_payment_document_ids.sorted()[:1]
        invoices = pay_results['invoices']

        # == Check PUE/PPD ==
        if (
            'PPD' not in set(invoices.mapped('l10n_mx_edi_payment_policy'))
        ):
            self._l10n_mx_edi_cfdi_payment_document_sent_pue(invoices)
            return
        # == Retry a cancellation flow ==
        if last_document.state == 'payment_cancel_failed':
            last_document._action_retry_payment_try_cancel()
            return

        qweb_template = self.env['l10n_mx_edi.document']._get_payment_cfdi_template()

        # == Lock ==
        self.env['res.company']._with_locked_records(self + invoices)

        # == Send ==
        def on_populate(cfdi_values):
            self._l10n_mx_edi_add_payment_cfdi_values(cfdi_values, pay_results)

        def on_failure(error, cfdi_filename=None, cfdi_str=None):
            self._l10n_mx_edi_cfdi_payment_document_sent_failed(error, invoices, cfdi_filename=cfdi_filename, cfdi_str=cfdi_str)

        def on_success(_cfdi_values, cfdi_filename, cfdi_str, populate_return=None):
            self._l10n_mx_edi_cfdi_payment_document_sent(invoices, cfdi_filename, cfdi_str)

        cfdi_filename = f'{self.journal_id.code}-{self.name}-MX-Payment-20.xml'.replace('/', '')
        self.env['l10n_mx_edi.document']._send_api(
            self.company_id,
            qweb_template,
            cfdi_filename,
            on_populate,
            on_failure,
            on_success,
        )
