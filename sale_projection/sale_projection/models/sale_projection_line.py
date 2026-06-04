# -*- coding: utf-8 -*-
import datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleProjectionLine(models.Model):
    """
    Core record of the Projection module.
    One record = one partner × one SKU × one ISO week.

    Fields:
    -------
    qty_projected   – manually entered by the user
    qty_scheduled   – computed from stock.move lines whose stock.picking.scheduled_date
                      falls within [week_start, week_end] and state in (confirmed, assigned)
    qty_executed    – computed from stock.move lines whose stock.picking remission_date
                      (or date_done if remission_date is absent) falls within the week
                      and picking state = done
    amount          – unit_price (from partner pricelist or last SO line) × qty_projected
    """
    _name = 'sale.projection.line'
    _description = 'Sale Projection Line'
    _order = 'week_start asc, partner_id, product_id'

    # ------------------------------------------------------------------
    # Relational fields
    # ------------------------------------------------------------------
    projection_id = fields.Many2one(
        comodel_name='sale.projection',
        string='Projection',
        ondelete='cascade',
        index=True,
    )

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Partner (Obra)',
        required=True,
        index=True,
    )

    constructora_id = fields.Many2one(
        comodel_name='res.partner',
        string='Construction Company',
        related='partner_id.parent_id',
        store=True,
        index=True,
    )

    product_id = fields.Many2one(
        comodel_name='product.product',
        string='SKU',
        required=True,
        index=True,
        domain=[('sale_ok', '=', True)],
    )

    product_uom_id = fields.Many2one(
        comodel_name='uom.uom',
        string='Unit of Measure',
        related='product_id.uom_id',
        store=True,
    )

    # ------------------------------------------------------------------
    # Week identification
    # ------------------------------------------------------------------
    week_start = fields.Date(
        string='Week Start (Monday)',
        required=True,
        index=True,
        help='ISO Monday of the projection week.',
    )

    week_end = fields.Date(
        string='Week End (Sunday)',
        required=True,
        help='ISO Sunday of the projection week.',
    )

    week_label = fields.Char(
        string='Week Label',
        compute='_compute_week_label',
        store=True,
        help='Human-readable label, e.g. "W23 / 2025".',
    )

    week_number = fields.Integer(
        string='ISO Week Number',
        compute='_compute_week_label',
        store=True,
    )

    year_number = fields.Integer(
        string='Year',
        compute='_compute_week_label',
        store=True,
    )

    # ------------------------------------------------------------------
    # Quantity fields
    # ------------------------------------------------------------------
    qty_projected = fields.Float(
        string='Projected Qty',
        digits='Product Unit of Measure',
        default=0.0,
        help='Manually entered projected quantity for this SKU and week.',
    )

    qty_scheduled = fields.Float(
        string='Scheduled Qty',
        digits='Product Unit of Measure',
        default=0.0,
        readonly=True,
        help='Sum of stock.move quantities whose picking.scheduled_date falls '
             'within this week and state in (confirmed, assigned, waiting).',
    )

    qty_executed = fields.Float(
        string='Executed Qty',
        digits='Product Unit of Measure',
        default=0.0,
        readonly=True,
        help='Sum of done stock.move quantities whose picking remission_date '
             '(or date_done) falls within this week.',
    )

    # ------------------------------------------------------------------
    # Amount / pricing
    # ------------------------------------------------------------------
    unit_price = fields.Float(
        string='Unit Price',
        digits='Product Price',
        default=0.0,
        help='Unit price from the partner pricelist or last sale order. '
             'Refreshed by the nightly cron.',
    )

    amount = fields.Float(
        string='Projected Amount',
        digits='Account',
        compute='_compute_amount',
        store=True,
        help='unit_price × qty_projected',
    )

    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        compute='_compute_currency_id',
        store=True,
    )

    # ------------------------------------------------------------------
    # Variance helpers (read-only, used in the board view)
    # ------------------------------------------------------------------
    variance_sched_proj = fields.Float(
        string='Variance Sched vs Proj',
        compute='_compute_variances',
        help='qty_scheduled − qty_projected',
    )

    variance_exec_proj = fields.Float(
        string='Variance Exec vs Proj',
        compute='_compute_variances',
        help='qty_executed − qty_projected',
    )

    # ------------------------------------------------------------------
    # Computed methods
    # ------------------------------------------------------------------

    @api.depends('week_start')
    def _compute_week_label(self):
        for rec in self:
            if rec.week_start:
                d = fields.Date.from_string(rec.week_start)
                iso = d.isocalendar()          # (year, week, weekday)
                rec.week_number = iso[1]
                rec.year_number = iso[0]
                rec.week_label = f'W{iso[1]:02d}/{iso[0]}'
            else:
                rec.week_number = 0
                rec.year_number = 0
                rec.week_label = ''

    @api.depends('partner_id', 'partner_id.property_product_pricelist')
    def _compute_currency_id(self):
        for rec in self:
            pricelist = rec.partner_id.property_product_pricelist
            rec.currency_id = pricelist.currency_id if pricelist else self.env.company.currency_id

    @api.depends('qty_projected', 'unit_price')
    def _compute_amount(self):
        for rec in self:
            rec.amount = rec.qty_projected * rec.unit_price

    @api.depends('qty_scheduled', 'qty_projected', 'qty_executed')
    def _compute_variances(self):
        for rec in self:
            rec.variance_sched_proj = rec.qty_scheduled - rec.qty_projected
            rec.variance_exec_proj = rec.qty_executed - rec.qty_projected

    # ------------------------------------------------------------------
    # Business logic: refresh scheduled & executed quantities
    # Called by the nightly cron and also available as a manual action.
    # ------------------------------------------------------------------

    def _get_stock_move_domain_base(self):
        """Return the base domain shared by scheduled and executed calculations."""
        self.ensure_one()
        return [
            ('product_id', '=', self.product_id.id),
            ('picking_id.partner_id', '=', self.partner_id.id),
            ('picking_id.picking_type_code', '=', 'outgoing'),
        ]

    def action_refresh_quantities(self):
        """Manually refresh qty_scheduled and qty_executed for selected lines."""
        for rec in self:
            rec._refresh_scheduled()
            rec._refresh_executed()
            rec._refresh_unit_price()

    def _refresh_scheduled(self):
        """
        Compute qty_scheduled:
        Sum product_uom_qty of stock.move lines where:
          - picking.scheduled_date is within [week_start, week_end]
          - picking state in ('confirmed', 'assigned', 'waiting')
        """
        self.ensure_one()
        Move = self.env['stock.move']
        domain = self._get_stock_move_domain_base() + [
            ('picking_id.scheduled_date', '>=', self.week_start),
            ('picking_id.scheduled_date', '<=', self.week_end),
            ('picking_id.state', 'in', ('confirmed', 'assigned', 'waiting')),
        ]
        result = Move.read_group(domain, ['product_uom_qty:sum'], [])
        self.qty_scheduled = result[0]['product_uom_qty'] if result and result[0]['product_uom_qty'] else 0.0

    def _refresh_executed(self):
        """
        Compute qty_executed:
        Sum qty_done of stock.move lines where:
          - picking remission_date (custom field) OR date_done falls within [week_start, week_end]
          - picking state = done

        We try the custom field 'remission_date' first; fall back to 'date_done'.
        """
        self.ensure_one()
        Move = self.env['stock.move']

        # Detect if stock.picking has a custom 'remission_date' field
        Picking = self.env['stock.picking']
        has_remission_date = 'remission_date' in Picking._fields

        if has_remission_date:
            domain = self._get_stock_move_domain_base() + [
                ('picking_id.remission_date', '>=', self.week_start),
                ('picking_id.remission_date', '<=', self.week_end),
                ('picking_id.state', '=', 'done'),
            ]
        else:
            # Fallback: use native date_done on stock.picking
            domain = self._get_stock_move_domain_base() + [
                ('picking_id.date_done', '>=', self.week_start),
                ('picking_id.date_done', '<=', self.week_end),
                ('picking_id.state', '=', 'done'),
            ]

        result = Move.read_group(domain, ['quantity:sum'], [])
        self.qty_executed = result[0]['quantity'] if result and result[0]['quantity'] else 0.0

    def _refresh_unit_price(self):
        """
        Determine unit_price using the following priority:
        1. Partner's pricelist (property_product_pricelist) → get_product_price()
        2. Most recent confirmed/done Sale Order line for this partner + product
        3. Product's standard sales price (list_price)
        """
        self.ensure_one()
        product = self.product_id
        partner = self.partner_id
        price = 0.0

        # 1. Pricelist
        pricelist = partner.property_product_pricelist
        if pricelist:
            price = pricelist._get_product_price(
                product=product,
                quantity=self.qty_projected or 1.0,
                currency=pricelist.currency_id,
                date=self.week_start,
            )

        # 2. Most recent SO line as fallback
        if not price:
            SaleLine = self.env['sale.order.line']
            last_line = SaleLine.search([
                ('order_id.partner_id', '=', partner.id),
                ('product_id', '=', product.id),
                ('order_id.state', 'in', ('sale', 'done')),
            ], order='order_id.date_order desc', limit=1)
            if last_line:
                price = last_line.price_unit

        # 3. Standard price
        if not price:
            price = product.lst_price

        self.unit_price = price

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------

    @api.model
    def cron_refresh_all_projections(self):
        """
        Nightly cron job.
        Refreshes scheduled qty, executed qty, and unit price for all
        projection lines whose week has not yet been fully executed
        (week_end >= today - 7 days to include the just-finished week).
        """
        cutoff = fields.Date.today() - datetime.timedelta(days=7)
        lines = self.search([('week_end', '>=', cutoff)])
        _logger_msg = f'[SaleProjection Cron] Refreshing {len(lines)} projection lines...'
        import logging
        _logger = logging.getLogger(__name__)
        _logger.info(_logger_msg)

        for line in lines:
            try:
                line._refresh_scheduled()
                line._refresh_executed()
                line._refresh_unit_price()
            except Exception as e:
                _logger.error(
                    f'[SaleProjection Cron] Error refreshing line {line.id}: {e}'
                )

        _logger.info('[SaleProjection Cron] Done.')

    # ------------------------------------------------------------------
    # Helper: week columns for the board view
    # ------------------------------------------------------------------

    @api.model
    def get_projection_board_data(self, partner_id, num_weeks=8):
        """
        Returns structured data for the JS projection board widget.

        Returns a dict with:
        - weeks: list of {label, week_start, week_end, is_past, is_current}
        - rows:  list of {product_id, product_name, cells: [{week_start, qty_projected,
                  qty_scheduled, qty_executed, amount, line_id}]}

        Weeks start from the previous ISO week (W-1 relative to today).
        """
        today = fields.Date.today()
        # Go back to the Monday of the previous week
        monday_current = today - datetime.timedelta(days=today.weekday())
        monday_start = monday_current - datetime.timedelta(weeks=1)

        weeks = []
        for i in range(num_weeks):
            ws = monday_start + datetime.timedelta(weeks=i)
            we = ws + datetime.timedelta(days=6)
            iso = ws.isocalendar()
            is_current = (ws == monday_current)
            is_past = (ws < monday_current)
            weeks.append({
                'label': f'W{iso[1]:02d}/{iso[0]}',
                'week_start': fields.Date.to_string(ws),
                'week_end': fields.Date.to_string(we),
                'is_past': is_past,
                'is_current': is_current,
            })

        # Fetch partner's SKU portfolio
        partner = self.env['res.partner'].browse(partner_id)
        products = partner.projection_product_ids

        if not products:
            # Fallback: show products from existing projection lines
            lines = self.search([('partner_id', '=', partner_id)])
            products = lines.mapped('product_id')

        # Fetch all relevant projection lines in one query
        week_starts = [w['week_start'] for w in weeks]
        all_lines = self.search([
            ('partner_id', '=', partner_id),
            ('week_start', 'in', week_starts),
        ])

        # Index lines by (product_id, week_start)
        line_index = {}
        for line in all_lines:
            key = (line.product_id.id, fields.Date.to_string(line.week_start))
            line_index[key] = line

        rows = []
        for product in products:
            cells = []
            for week in weeks:
                key = (product.id, week['week_start'])
                line = line_index.get(key)
                cells.append({
                    'week_start': week['week_start'],
                    'qty_projected': line.qty_projected if line else 0.0,
                    'qty_scheduled': line.qty_scheduled if line else 0.0,
                    'qty_executed': line.qty_executed if line else 0.0,
                    'amount': line.amount if line else 0.0,
                    'unit_price': line.unit_price if line else 0.0,
                    'line_id': line.id if line else False,
                    'week_label': week['label'],
                })
            rows.append({
                'product_id': product.id,
                'product_name': product.display_name,
                'product_ref': product.default_code or '',
                'cells': cells,
            })

        return {
            'partner_id': partner_id,
            'partner_name': partner.display_name,
            'weeks': weeks,
            'rows': rows,
        }

    @api.model
    def save_projected_qty(self, partner_id, product_id, week_start, qty_projected):
        """
        RPC method called by the JS board widget when the user edits a cell.
        Creates the projection line if it does not exist yet.
        """
        week_start_date = fields.Date.from_string(week_start)
        week_end_date = week_start_date + datetime.timedelta(days=6)

        line = self.search([
            ('partner_id', '=', partner_id),
            ('product_id', '=', product_id),
            ('week_start', '=', week_start),
        ], limit=1)

        if line:
            line.qty_projected = qty_projected
        else:
            line = self.create({
                'partner_id': partner_id,
                'product_id': product_id,
                'week_start': week_start,
                'week_end': fields.Date.to_string(week_end_date),
                'qty_projected': qty_projected,
            })
            line._refresh_unit_price()

        return {
            'line_id': line.id,
            'amount': line.amount,
            'unit_price': line.unit_price,
        }
