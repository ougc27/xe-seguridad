from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    pos_session_id = fields.Many2one(
        'pos.session',
        readonly=True
    )
