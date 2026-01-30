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
        help="The fiscal name of the customer with which the invoice will be issued to the SAT.")

    def _get_unspsc_code_for_partner(self, product):
        self.ensure_one()

        clientinfo = self.env['product.clientinfo'].sudo().search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('name', '=', self.id),
            ('unspsc_code_id', '!=', False),
        ], limit=1)

        if clientinfo:
            return clientinfo.unspsc_code_id.code

        return product.unspsc_code_id.code
