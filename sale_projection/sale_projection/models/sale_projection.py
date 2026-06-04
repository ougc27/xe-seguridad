# -*- coding: utf-8 -*-
from odoo import api, fields, models


class SaleProjection(models.Model):
    """
    Header record for a projection planning session.
    Groups projection lines for a given partner and planning period.
    This model is optional but provides a convenient grouping/status header.
    """
    _name = 'sale.projection'
    _description = 'Sale Projection'
    _order = 'date_from desc, partner_id'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        default=lambda self: self.env['ir.sequence'].next_by_code('sale.projection') or 'New',
    )

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Partner (Obra)',
        required=True,
        index=True,
        domain=[('is_obra', '=', True)],
        help='Construction site (obra) for which this projection is created.',
    )

    # Constructora (parent) — auto-filled from the obra's parent
    constructora_id = fields.Many2one(
        comodel_name='res.partner',
        string='Construction Company',
        compute='_compute_constructora_id',
        store=True,
        index=True,
    )

    date_from = fields.Date(
        string='Period Start',
        required=True,
        help='First day of the planning horizon (usually Monday of week W-1).',
    )

    date_to = fields.Date(
        string='Period End',
        required=True,
        help='Last day of the planning horizon.',
    )

    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('confirmed', 'Confirmed'),
            ('done', 'Done'),
        ],
        string='Status',
        default='draft',
        tracking=True,
    )

    note = fields.Text(string='Notes')

    line_ids = fields.One2many(
        comodel_name='sale.projection.line',
        inverse_name='projection_id',
        string='Projection Lines',
    )

    line_count = fields.Integer(
        string='Lines',
        compute='_compute_line_count',
    )

    # ------------------------------------------------------------------
    # Computed fields
    # ------------------------------------------------------------------

    @api.depends('partner_id', 'partner_id.parent_id')
    def _compute_constructora_id(self):
        for rec in self:
            rec.constructora_id = rec.partner_id.parent_id if rec.partner_id else False

    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_confirm(self):
        self.write({'state': 'confirmed'})

    def action_done(self):
        self.write({'state': 'done'})

    def action_draft(self):
        self.write({'state': 'draft'})

    def action_generate_lines(self):
        """
        Auto-generate projection lines for each SKU in the partner's portfolio
        and each week in [date_from, date_to].
        Existing lines for the same (partner, product, week_start) are skipped.
        """
        self.ensure_one()
        if not self.partner_id.projection_product_ids:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No SKUs in portfolio',
                    'message': 'Add products to the partner\'s Projection SKU Portfolio first.',
                    'type': 'warning',
                },
            }

        Line = self.env['sale.projection.line']
        current = fields.Date.from_string(self.date_from)
        end = fields.Date.from_string(self.date_to)
        import datetime

        # Advance to Monday of the start week
        current -= datetime.timedelta(days=current.weekday())

        while current <= end:
            week_end = current + datetime.timedelta(days=6)
            for product in self.partner_id.projection_product_ids:
                existing = Line.search([
                    ('partner_id', '=', self.partner_id.id),
                    ('product_id', '=', product.id),
                    ('week_start', '=', fields.Date.to_string(current)),
                ], limit=1)
                if not existing:
                    Line.create({
                        'projection_id': self.id,
                        'partner_id': self.partner_id.id,
                        'product_id': product.id,
                        'week_start': fields.Date.to_string(current),
                        'week_end': fields.Date.to_string(week_end),
                    })
            current += datetime.timedelta(weeks=1)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Lines Generated',
                'message': 'Projection lines have been created successfully.',
                'type': 'success',
            },
        }
