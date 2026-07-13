from odoo import api, fields, models


class StockLot(models.Model):
    _inherit = 'stock.lot'

    blocked = fields.Boolean(
        string="Blocked for Transfers",
        default=False,
        copy=False,
        help="If enabled, this lot/serial is hidden from selection and cannot "
             "be sent to Transit or validated in transfers until unblocked.",
    )
