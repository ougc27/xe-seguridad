from odoo import models, fields, api


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

    def l10n_mx_edi_cfdi_invoice_try_update_payment(self, pay_results, force_cfdi=False):
        """ Update the CFDI state of the current payment.

        :param pay_results: The amounts to consider for each invoice.
                            See '_l10n_mx_edi_cfdi_payment_get_reconciled_invoice_values'.
        :param force_cfdi:  Force the sending of the CFDI if the payment is PUE.
        """
        self.ensure_one()

        last_document = self.l10n_mx_edi_payment_document_ids.sorted()[:1]
        invoices = pay_results['invoices']

        # == Check PUE/PPD ==
        if (
            'PPD' not in set(invoices.mapped('l10n_mx_edi_payment_policy'))
        ):
            self._l10n_mx_edi_cfdi_payment_document_sent_pue(invoices)
            return
        # == Retry a cancellation flow ==
        if last_document.state == 'payment_cancel_failed':
            last_document._action_retry_payment_try_cancel()
            return

        qweb_template = self.env['l10n_mx_edi.document']._get_payment_cfdi_template()

        # == Lock ==
        self.env['res.company']._with_locked_records(self + invoices)

        # == Send ==
        def on_populate(cfdi_values):
            self._l10n_mx_edi_add_payment_cfdi_values(cfdi_values, pay_results)

        def on_failure(error, cfdi_filename=None, cfdi_str=None):
            self._l10n_mx_edi_cfdi_payment_document_sent_failed(error, invoices, cfdi_filename=cfdi_filename, cfdi_str=cfdi_str)

        def on_success(_cfdi_values, cfdi_filename, cfdi_str, populate_return=None):
            self._l10n_mx_edi_cfdi_payment_document_sent(invoices, cfdi_filename, cfdi_str)

        cfdi_filename = f'{self.journal_id.code}-{self.name}-MX-Payment-20.xml'.replace('/', '')
        self.env['l10n_mx_edi.document']._send_api(
            self.company_id,
            qweb_template,
            cfdi_filename,
            on_populate,
            on_failure,
            on_success,
        )
