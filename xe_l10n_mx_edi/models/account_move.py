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
