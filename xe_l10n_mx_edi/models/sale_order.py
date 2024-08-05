from odoo import fields, models
from.l10n_mx_edi_document import USAGE_SELECTION


class SaleOrder(models.Model):
    
    _inherit = 'sale.order'

    l10n_mx_edi_usage = fields.Selection(
        selection=USAGE_SELECTION,
        string="Usage",
        compute="_compute_l10n_mx_edi_usage",
        store=True,
        readonly=False,
        default="G03",
        tracking=True,
        help="The code that corresponds to the use that will be made of the receipt by the recipient.",
    )
