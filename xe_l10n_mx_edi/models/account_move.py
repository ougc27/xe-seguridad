from odoo import fields, models
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
