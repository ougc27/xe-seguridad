from odoo import api, fields, models


class CancelledRemission(models.Model):
    _name = 'cancelled.remission'
    _description = 'Cancelled Remissions from stock.picking'
    _order = 'cancelled_date desc, id desc'

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

    remission_date = fields.Datetime(
        string="Remission Date",
        readonly=True,
        help="Date of the remission before it was cancelled")

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

    partner_id = fields.Many2one(
        'res.partner',
        string="Customer",
        related='picking_id.partner_id',
        store=True,
        readonly=True,
        help="Customer taken from the transfer folio.")

    team_id = fields.Char(
        string="Sales Team",
        related='picking_id.x_studio_canal_de_distribucin',
        store=True,
        readonly=True,
        help="Sales team taken from the transfer folio.")

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string="Warehouse",
        related='picking_id.location_id.warehouse_id',
        store=True,
        readonly=True,
        help="Warehouse taken from the transfer folio.")

    is_from_ticket = fields.Boolean(
        string="From Ticket",
        compute='_compute_is_from_ticket',
        store=True,
        help="Indicates if the transfer folio was generated from a helpdesk ticket.")

    company_id = fields.Many2one(
        'res.company', 'Company', required=True, index=True,
        default=lambda self: self.env.company)

    @api.depends('picking_id.service_ticket_id', 'picking_id.helpdesk_ticket_ids')
    def _compute_is_from_ticket(self):
        for record in self:
            record.is_from_ticket = bool(
                record.picking_id.service_ticket_id or record.picking_id.helpdesk_ticket_ids)
