from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_pos_store = fields.Boolean()
