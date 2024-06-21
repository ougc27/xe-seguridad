from odoo import models, fields

class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_border_zone_iva = fields.Boolean(
        help='Indicates if the contact is eligible for the reduced 8% VAT rate.')
