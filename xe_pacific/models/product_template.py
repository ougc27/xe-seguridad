from odoo import models, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    not_automatic_lot_number = fields.Boolean()

    block_return = fields.Boolean(
        string="Blocked for Returns",
        copy=False,
        readonly=True,
        help="While enabled, returns of this product cannot be validated. "
             "Set automatically by purchase receptions flagged as blocked.",
    )
