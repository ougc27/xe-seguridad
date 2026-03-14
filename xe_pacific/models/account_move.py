from odoo import models, fields, api, _



class AccountMove(models.Model):
    _inherit = 'account.move'

    pos_store = fields.Many2one('res.partner', 
        related='x_order_id.pos_store',
        readonly=True,
        store=True
    )

    picking_ids = fields.Many2many(
        'stock.picking',
        'account_move_picking_rel',
        'move_id',
        'picking_id',
        string='Remissions',
        #context={'from_helpdesk_ticket': True},
        help='Relationship between referrals and invoice.'
    )

    x_order_id = fields.Many2one('sale.order',
        related='invoice_line_ids.x_order_id',
        store=True,
        readonly=True
    )

    x_studio_almacen_id = fields.Many2one(
        'stock.warehouse',
        'Warehouse',
        compute='_compute_warehouse_id',
        readonly=True,
        store=True,
        tracking=True
    )

    warehouse_id = fields.Many2one(
        'stock.warehouse',
        'Warehouse',
    )

    order_ids = fields.Many2many(
        'sale.order',
        'account_move_sale_order_rel',
        'move_id',
        'order_id',
        string='Sale Orders',
        compute='_compute_order_ids'
    )

    @api.depends('line_ids', 'x_order_id', 'warehouse_id')
    def _compute_warehouse_id(self):
        for record in self:
            if record.warehouse_id:
                record['x_studio_almacen_id'] = record.warehouse_id
                continue
            order_id = record.x_order_id
            if order_id:
                record['x_studio_almacen_id'] = order_id.warehouse_id.id
            if record.purchase_order_count >= 1:
                record['x_studio_almacen_id'] = record.line_ids[0].purchase_order_id.picking_type_id.warehouse_id.id
            if record.pos_order_ids:
                pos_warehouse_id = record.pos_order_ids[0].config_id.picking_type_id.warehouse_id.id
                record['x_studio_almacen_id'] = pos_warehouse_id
                record['warehouse_id'] = pos_warehouse_id

    @api.depends('invoice_origin')
    def _compute_order_ids(self):
        for record in self:
            if record.invoice_origin:
                order_names = [name.strip() for name in record.invoice_origin.split(',')]
                record.order_ids = self.env['sale.order'].sudo().search(
                    [('company_id', '=', self.env.company.id), ('name', 'in', order_names)]).ids
                continue
            record.order_ids = False

    @api.depends(
        'move_type',
        'invoice_date_due',
        'invoice_date',
        'invoice_payment_term_id',
        'l10n_mx_edi_payment_method_id',
    )
    def _compute_l10n_mx_edi_payment_policy(self):
        res = super(AccountMove, self)._compute_l10n_mx_edi_payment_policy()
        for move in self:

            if not move.l10n_mx_edi_payment_method_id:
                continue

            if move.l10n_mx_edi_payment_method_id.code == '99':
                move.l10n_mx_edi_payment_policy = 'PPD'
            else:
                move.l10n_mx_edi_payment_policy = 'PUE'
        return res

    def _compute_payments_widget_to_reconcile_info(self):
        for move in self:
            move.invoice_outstanding_credits_debits_widget = False
            move.invoice_has_outstanding = False

            if move.state != 'posted' \
                    or move.payment_state not in ('not_paid', 'partial') \
                    or not move.is_invoice(include_receipts=True):
                continue

            pay_term_lines = move.line_ids\
                .filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))

            partner_ids = [move.commercial_partner_id.id]
            if move.company_id.id == 3:
                warehouse_id = move.x_studio_almacen_id.id
                warehouse_partner_id = self.env['pos.config'].sudo().search(
                    [('warehouse_id', '=', warehouse_id)], limit=1
                ).delivery_partner_id.id
                if warehouse_partner_id:
                    partner_ids.append(warehouse_partner_id)
            domain = [
                ('account_id', 'in', pay_term_lines.account_id.ids),
                ('parent_state', '=', 'posted'),
                ('partner_id', 'in', partner_ids),
                ('reconciled', '=', False),
                '|', ('amount_residual', '!=', 0.0), ('amount_residual_currency', '!=', 0.0),
            ]

            payments_widget_vals = {'outstanding': True, 'content': [], 'move_id': move.id}

            if move.is_inbound():
                domain.append(('balance', '<', 0.0))
                payments_widget_vals['title'] = _('Outstanding credits')
            else:
                domain.append(('balance', '>', 0.0))
                payments_widget_vals['title'] = _('Outstanding debits')

            for line in self.env['account.move.line'].search(domain):

                if line.currency_id == move.currency_id:
                    # Same foreign currency.
                    amount = abs(line.amount_residual_currency)
                else:
                    # Different foreign currencies.
                    amount = line.company_currency_id._convert(
                        abs(line.amount_residual),
                        move.currency_id,
                        move.company_id,
                        line.date,
                    )

                if move.currency_id.is_zero(amount):
                    continue

                payments_widget_vals['content'].append({
                    'journal_name': line.ref or line.move_id.name,
                    'amount': amount,
                    'currency_id': move.currency_id.id,
                    'id': line.id,
                    'move_id': line.move_id.id,
                    'date': fields.Date.to_string(line.date),
                    'account_payment_id': line.payment_id.id,
                })

            if not payments_widget_vals['content']:
                continue

            move.invoice_outstanding_credits_debits_widget = payments_widget_vals
            move.invoice_has_outstanding = True
