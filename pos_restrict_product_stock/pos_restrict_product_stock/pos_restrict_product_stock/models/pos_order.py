from odoo import models, api, _


class PosOrder(models.Model):
    _inherit = 'pos.order'

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
        order = order['data']
        pos_session = self.env['pos.session'].browse(order['pos_session_id'])
        if pos_session.state == 'closing_control' or pos_session.state == 'closed':
            order['pos_session_id'] = self._get_valid_session(order).id

        if order.get('shipping_date'):
            order['partner_id'] = pos_session.config_id.delivery_partner_id.id

        else:
            if order.get('partner_id'):
                partner_id = self.env['res.partner'].browse(order['partner_id'])
                if not partner_id.exists():
                    order.update({
                        "partner_id": False,
                        "to_invoice": False,
                    })

        pos_order = False
        if not existing_order:
            pos_order = self.create(self._order_fields(order))
        else:
            pos_order = existing_order
            pos_order.lines.unlink()
            order['user_id'] = pos_order.user_id.id
            pos_order.write(self._order_fields(order))

        pos_order._link_combo_items(order)
        pos_order = pos_order.with_company(pos_order.company_id)
        self = self.with_company(pos_order.company_id)
        self._process_payment_lines(order, pos_order, pos_session, draft)
        return pos_order._process_saved_order(draft)


    def get_res_partner_by_id(self):
        partner = self.env['res.partner'].browse(self.id)
        return [partner.name, partner.street, partner.city, partner.country_id.name]

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

            order.write({'account_move': new_move.id, 'state': 'invoiced'})
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
