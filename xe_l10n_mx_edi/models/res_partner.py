from odoo import models, fields
from .l10n_mx_edi_document import USAGE_SELECTION


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_border_zone_iva = fields.Boolean(
        help='Indicates if the contact is eligible for the reduced 8% VAT rate.')

    l10n_mx_edi_usage = fields.Selection(
        selection=USAGE_SELECTION,
        string="Usage",
        help="The code that corresponds to the use that will be made of the receipt by the recipient.",
    )

    fiscal_name = fields.Char(
        help="The fiscal name of the customer with which the invoice will be issued to the SAT."
    )
    