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

    def action_pos_order_invoice(self):
        if len(self.company_id) > 1:
            raise UserError(_("You cannot invoice orders belonging to different companies."))
        self.write({'to_invoice': True})
        if self.company_id.anglo_saxon_accounting and self.session_id.update_stock_at_closing and self.session_id.state != 'closed':
            self._create_order_picking()
        with_context = self.env.context.copy()
        with_context.update({'from_portal': True})
        return self.with_context(**with_context)._generate_pos_order_invoice()

    def _generate_pos_order_invoice(self):
        moves = self.env['account.move']

        for order in self:
            # Force company for all SUPERUSER_ID action
            if order.account_move:
                moves += order.account_move
                continue

            if not order.partner_id:
                raise UserError(_('Please provide a partner for the sale.'))

            move_vals = order._prepare_invoice_vals()
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

            moves += new_move
            new_move.write({'payment_state': 'in_payment'})

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
