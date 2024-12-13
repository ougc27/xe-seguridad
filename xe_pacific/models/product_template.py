from odoo import models, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    not_automatic_lot_number = fields.Boolean()
