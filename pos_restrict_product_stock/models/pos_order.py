import pytz

from odoo import models, fields, api, _
from datetime import timedelta, datetime
from odoo.exceptions import UserError


class PosOrder(models.Model):
    _inherit = 'pos.order'

    reference = fields.Char(string='Physical Order', copy=False, readonly=True)

    payment_proof_attachments = fields.Many2many(
        'ir.attachment',
        'pos_order_payment_proof_attachment_rel',
        'pos_order_id',
        'attachment_id',
    )

    to_invoice = fields.Boolean('To invoice', copy=False, compute="_compute_to_invoice", store=True)

    is_refunded = fields.Boolean(compute='_compute_refund_related_fields', store=True)

    refund_orders_count = fields.Integer('Number of Refund Orders', compute='_compute_refund_related_fields', store=True)

    paid_commission = fields.Boolean(copy=False)

    amount_untaxed = fields.Float(compute='_compute_amount_untaxed', string='Untaxed Amount', store=True)

    @api.depends('payment_ids', 'lines')
    def _compute_amount_untaxed(self):
        for order in self:
            if not order.currency_id:
                raise UserError(_("You can't: create a pos order from the backend interface, or unset the pricelist, or create a pos.order in a python test with Form tool, or edit the form view in studio if no PoS order exist"))
            currency = order.currency_id
            amount_untaxed = currency.round(sum(line.price_subtotal for line in order.lines))
            order.amount_untaxed = amount_untaxed

    @api.depends('account_move.state')
    def _compute_to_invoice(self):
        for record in self:
            account_move = record.account_move
            if account_move:
                if account_move.state == 'posted':
                    record.to_invoice = True
                    record.state = 'invoiced'
                    continue
            record.to_invoice = False
            if record.state in ('draft', 'cancel'):
                continue
            record.state = 'done'

    @api.model
    def _process_order(self, order, draft, existing_order):
        """Create or update an pos.order from a given dictionary.

        :param dict order: dictionary representing the order.
        :param bool draft: Indicate that the pos_order is not validated yet.
        :param existing_order: order to be updated or False.
        :type existing_order: pos.order.
        :returns: id of created/updated pos.order
        :rtype: int
        """
        order_id = super(PosOrder, self)._process_order(order, draft, existing_order)
        order = self.browse(order_id)
        delivery_partner_id = order.session_id.config_id.delivery_partner_id.id
        if delivery_partner_id:
            #if not order.shipping_date:
                #order.write({'partner_id': delivery_partner_id})
            for picking in order.picking_ids:
                picking.write({'partner_id': delivery_partner_id})

    def get_res_partner_by_id(self):
        partner = self.env['res.partner'].browse(self.id)
        return [partner.name, partner.street, partner.city, partner.country_id.name]

    def action_pos_order_invoice(self, partner_id=False):
        if len(self.company_id) > 1:
            raise UserError(_("You cannot invoice orders belonging to different companies."))
        self.write({'to_invoice': True})
        if self.company_id.anglo_saxon_accounting and self.session_id.update_stock_at_closing and self.session_id.state != 'closed':
            self._create_order_picking()
        with_context = self.env.context.copy()
        with_context.update({'from_portal': True})
        return self.with_context(**with_context)._generate_pos_order_invoice(partner_id)

    def _generate_pos_order_invoice(self, partner_id=False):
        moves = self.env['account.move']

        for order in self:
            # Force company for all SUPERUSER_ID action
            if order.account_move:
                moves += order.account_move
                continue

            if not order.partner_id:
                raise UserError(_('Please provide a partner for the sale.'))

            move_vals = order._prepare_invoice_vals(partner_id)
            new_move = order._create_invoice(move_vals)

            order.write({'account_move': new_move.id})
            new_move.sudo().with_company(order.company_id).with_context(skip_invoice_sync=True)._post()

            payment_moves = order._apply_invoice_payments(order.session_id.state == 'closed')

            # Send and Print
            if self.env.context.get('generate_pdf', True):
                template = self.env.ref(new_move._get_mail_template())
                new_move.with_context(skip_invoice_sync=True)._generate_pdf_and_send_invoice(template)


            if order.session_id.state == 'closed':  # If the session isn't closed this isn't needed.
                # If a client requires the invoice later, we need to revers the amount from the closing entry, by making a new entry for that.
                order._create_misc_reversal_move(payment_moves)

            new_move.partner_id.write({
                'team_id': order.config_id.crm_team_id.id
            })
            moves += new_move
            new_move.write({
                'payment_state': 'in_payment',
                'pos_session_id': order.session_id.id,
            })

        if not moves:
            return {}

        if self.env.context.get('from_portal'):
            group = self.env.ref('pos_restrict_product_stock.group_portal_invoice_auto_followers', raise_if_not_found=False)
            if group:
                users = group.users
                if users:
                    for move in moves:
                        partner_ids = users.mapped('partner_id').ids
                        move.sudo().message_subscribe(partner_ids)

        return {
            'name': _('Customer Invoice'),
            'view_mode': 'form',
            'view_id': self.env.ref('account.view_move_form').id,
            'res_model': 'account.move',
            'context': "{'move_type':'out_invoice'}",
            'type': 'ir.actions.act_window',
            'nodestroy': True,
            'target': 'current',
            'res_id': moves and moves.ids[0] or False,
        }

    def _apply_invoice_payments(self, is_reverse=False):
        receivable_account = self.env["res.partner"]._find_accounting_partner(self.partner_id).with_company(self.company_id).property_account_receivable_id
        payment_moves = self.payment_ids.sudo().with_company(self.company_id)._create_payment_moves(is_reverse)
        if receivable_account.reconcile:
            invoice_receivables = self.account_move.line_ids.filtered(lambda line: line.account_id == receivable_account and not line.reconciled)
            if invoice_receivables:
                payment_receivables = payment_moves.mapped('line_ids').filtered(lambda line: line.account_id == receivable_account and line.partner_id)
                (invoice_receivables | payment_receivables).sudo().with_company(self.company_id).reconcile()
        return payment_moves

    @api.model
    def _order_fields(self, ui_order):
        order_fields = super(PosOrder, self)._order_fields(ui_order)
        order_fields['reference'] = ui_order.get('reference', False)
        return order_fields

    def action_open_available_to_invoice_orders(self):
        three_days_ago = fields.Datetime.to_string(datetime.now() - timedelta(days=3))
        return {
            'type': 'ir.actions.act_window',
            'name': 'Órdenes Disponibles para Facturar',
            'res_model': 'pos.order',
            'view_mode': 'tree,form',
            'domain': [
                ('to_invoice', '=', False),
                ('date_order', '<=', three_days_ago),
                '|',
                ('is_refunded', '=', False),
                ('refund_orders_count', '=', 0),
            ],
            'context': {
                'limit': 20,
            },
        }

    def action_receipt_to_customer(self, name, client, ticket):
        self = self.search([('pos_reference', '=', name), ('company_id', '=', self.env.company.id)], limit=1)
        if not self:
            return False
        if not client.get('email'):
            return False

        mail = self.env['mail.mail'].sudo().create(self._prepare_mail_values(name, client, ticket))
        mail.send()

    def _prepare_invoice_vals(self, partner_id=False):
        self.ensure_one()
        partner_id = partner_id or self.partner_id
        timezone = pytz.timezone(self._context.get('tz') or self.env.user.tz or 'UTC')
        invoice_date = fields.Datetime.now() if self.session_id.state == 'closed' else self.date_order
        pos_refunded_invoice_ids = []
        for orderline in self.lines:
            if orderline.refunded_orderline_id and orderline.refunded_orderline_id.order_id.account_move:
                pos_refunded_invoice_ids.append(orderline.refunded_orderline_id.order_id.account_move.id)
        vals = {
            'invoice_origin': self.name,
            'pos_refunded_invoice_ids': pos_refunded_invoice_ids,
            'journal_id': self.session_id.config_id.invoice_journal_id.id,
            'move_type': 'out_invoice' if self.amount_total >= 0 else 'out_refund',
            'ref': self.name,
            'partner_id': partner_id.address_get(['invoice'])['invoice'],
            'partner_bank_id': self._get_partner_bank_id(),
            'currency_id': self.currency_id.id,
            'invoice_user_id': self.user_id.id,
            'invoice_date': invoice_date.astimezone(timezone).date(),
            'fiscal_position_id': self.fiscal_position_id.id,
            'invoice_line_ids': self._prepare_invoice_lines(),
            'invoice_payment_term_id': False,
            'invoice_cash_rounding_id': self.config_id.rounding_method.id
            if self.config_id.cash_rounding and (not self.config_id.only_round_cash_method or any(p.payment_method_id.is_cash_count for p in self.payment_ids))
            else False
        }
        if self.refunded_order_ids.account_move:
            vals['ref'] = _('Reversal of: %s', self.refunded_order_ids.account_move.name)
            vals['reversed_entry_id'] = self.refunded_order_ids.account_move.id
        if self.note:
            vals.update({'narration': self.note})
        return vals
