from odoo import fields, models


class CancelledRemission(models.Model):
    _name = 'cancelled.remission'
    _description = 'Cancelled Remissions from stock.picking'

    picking_id = fields.Many2one(
        'stock.picking', 'Transfer Folio',
        check_company=True,
        readonly=True,
        index=True,
        help='The transfer folio where the remission folio was cancelled')

    remission_folio = fields.Char(
        string="Remission Folio",
        copy=False,
        readonly=True,
        help="Cancelled remission folio from transfer")

    cancelled_date = fields.Datetime(
        string="Cancelled Date",
        readonly=True,
        default=fields.Datetime.now,
        help="Cancelled date of the remission folio")

    user_id = fields.Many2one(
        'res.users',
        string="Cancelled By",
        readonly=True,
        default=lambda self: self.env.user)

    cancelled_reason = fields.Many2one(
        'cancelled.remission.reason',
        string="Cancellation Reason",
        readonly=True,
        help="Reason for cancelling the remission.")

    comments = fields.Html(
        string="Observations",
        readonly=True,
        help="Additional details or observations about the cancellation.")

    tag_ids = fields.Many2many('inventory.tag',
        string="Tags",
        readonly=True)
