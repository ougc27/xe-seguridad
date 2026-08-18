import pytz
from collections import defaultdict
from odoo import models, fields, api, _
from datetime import timedelta, datetime
from odoo.exceptions import UserError
from odoo.tools import float_compare


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

    original_partner_id = fields.Many2one(
        "res.partner",
        string="Original Customer",
        readonly=True,
        index=True,
        help="Customer selected by the salesperson in the Point of Sale at the time of the sale. "
            "This field preserves the original customer information even if the partner on the order "
            "is later changed (for example, during global invoicing)."
    )

    has_payment_proof = fields.Boolean(
        compute="_compute_has_payment_proof",
    )

    invoice_history_ids = fields.Many2many(
        'account.move',
        'pos_order_invoice_history_rel',
        'order_id',
        'move_id',
        string='Invoice History',
        copy=False,
        help="Keeps track of every accounting move related to this order's invoicing "
             "(the invoice itself, credit notes, cancellation/reversal entries, etc.), "
             "even when the original invoice link (account_move) has changed or been replaced."
    )

    invoice_history_count = fields.Integer(
        string='Invoice History Count',
        compute='_compute_invoice_history_count',
    )

    def _compute_has_payment_proof(self):
        for rec in self:
            rec.has_payment_proof = bool(rec.payment_proof_attachments)

    @api.depends('invoice_history_ids')
    def _compute_invoice_history_count(self):
        for order in self:
            order.invoice_history_count = len(order.invoice_history_ids)

    def _add_to_invoice_history(self, moves):
        """Register one or more account.move records in this order's invoice
        history, and make sure they carry this order's session so they also
        show up in the session's related journal entries (see
        pos.session._get_related_account_moves, which filters by
        pos_session_id).
        """
        self.ensure_one()
        moves = moves.filtered(lambda m: m.id)
        if not moves:
            return
        self.sudo().write({'invoice_history_ids': [(4, move.id) for move in moves]})
        moves_without_session = moves.filtered(lambda m: not m.pos_session_id)
        if moves_without_session and self.session_id:
            moves_without_session.sudo().write({'pos_session_id': self.session_id.id})

    def action_view_invoice_history(self):
        self.ensure_one()
        moves = self.invoice_history_ids
        action = {
            'name': _('Invoice History'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'context': {'create': False},
        }
        if len(moves) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': moves.id,
            })
        else:
            action.update({
                'view_mode': 'tree,form',
                'domain': [('id', 'in', moves.ids)],
            })
        return action


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
        coupon_lines = order.lines.filtered(lambda l: l.coupon_id)
        for line in coupon_lines:
            if not line.coupon_id.program_id.allow_multiple_use:
                line.coupon_id.sudo().write({'source_pos_order_id': order_id})

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

            order.partner_id.write({'property_account_receivable_id': order.config_id.property_account_receivable_id.id})

            move_vals = order._prepare_invoice_vals(partner_id)
            # Modo "IVA incluido en POS": solo aplica a ordenes NUEVAS (cuyo
            # price_unit ya trae IVA). Las ordenes viejas guardaron price_unit
            # como base sin IVA y deben facturarse por fuera como se crearon.
            pos_incl = bool(order.config_id.tax_id.pos_price_include) and order._pos_is_tax_included_order()
            new_move = order.with_context(pos_price_include_mode=pos_incl)._create_invoice(move_vals)
            new_move.write({
                'l10n_mx_edi_payment_method_id': order.l10n_mx_edi_payment_method_id.id,
            })

            order.write({'account_move': new_move.id})
            order._add_to_invoice_history(new_move)
            new_move.sudo().with_company(order.company_id).with_context(skip_invoice_sync=True, pos_price_include_mode=pos_incl)._post()

            payment_moves = order._apply_invoice_payments(order.session_id.state == 'closed')

            # Send and Print
            if self.env.context.get('generate_pdf', True):
                template = self.env.ref(new_move._get_mail_template())
                new_move.with_context(skip_invoice_sync=True)._generate_pdf_and_send_invoice(template)


            if order.session_id.state == 'closed':  # If the session isn't closed this isn't needed.
                # If a client requires the invoice later, we need to revers the amount from the closing entry, by making a new entry for that.
                # Nota: este asiento es misceláneo (POSS), no factura ni nota de
                # crédito, por lo que no se agrega a invoice_history_ids.
                order._create_misc_reversal_move(payment_moves)

            new_move.partner_id.write({
                'team_id': order.config_id.crm_team_id.id,
                'user_id': order.user_id.id
            })
            moves += new_move
            new_move.write({
                'payment_state': 'in_payment',
                'pos_session_id': order.session_id.id,
                'team_id': order.config_id.crm_team_id.id,
                'invoice_user_id': order.user_id.id,
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

    @api.depends('name', 'reference')
    @api.depends_context('from_helpdesk_ticket')
    def _compute_display_name(self):
        if not self._context.get('from_helpdesk_ticket'):
            return super()._compute_display_name()
        for rec in self:
            name = rec.name
            if rec.reference:
                name = f'{name} - {rec.reference}'
            rec.display_name = name

    @api.model
    def _name_search(self, name, domain=None, operator='ilike', limit=None, order=None):
        if self.env.context.get('from_helpdesk_ticket'):
            query = """
                SELECT id
                    FROM pos_order
                    WHERE
                    (
                        name ilike %s
                    OR
                        reference ilike %s
                    )
                    AND
                        company_id = %s
                    LIMIT %s
            """
            self.env.cr.execute(
                query, (
                    '%' + name + '%',
                    '%' + name + '%',
                    self.env.company.id,
                    limit
                )
            )
            return [row[0] for row in self.env.cr.fetchall()]
        return super()._name_search(name, domain, operator, limit, order)

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        for order in orders:
            partner_id = order.partner_id.id
            if partner_id:
                order.sudo().write({'original_partner_id': partner_id})
        return orders

    def check_moves(self):
        """Checks that the order cannot be cancelled if there are undelivered or unreturned products."""
    
        qty_out = {}
        qty_returned = {}
        
        pickings_in_transit = self.picking_ids.filtered(lambda p: p.state in ('transit'))
        if pickings_in_transit:
            raise UserError(_(
                "You cannot cancel this order. This order has a picking or pickings in transit state."
            ))

        pickings_to_cancel = self.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        if pickings_to_cancel:
            pickings_to_cancel.sudo().action_cancel()
        
        for picking in self.picking_ids.filtered(lambda p: p.state == 'done'):
            for line in picking.move_line_ids:
                if line.location_id.usage == 'internal' and line.location_dest_id.usage == 'customer':
                    qty_out[line.product_id] = qty_out.get(line.product_id, 0) + line.qty_done
    
                elif line.location_id.usage == 'customer' and line.location_dest_id.usage == 'internal':
                    qty_returned[line.product_id] = qty_returned.get(line.product_id, 0) + line.qty_done
    
        for product, qty_delivered in qty_out.items():
            qty_ret = qty_returned.get(product, 0)
            if qty_ret != qty_delivered:
                raise UserError(_(
                    "You cannot cancel this order. The product %s has %s units delivered and %s units returned."
                ) % (product.display_name, qty_delivered, qty_ret))

    def check_invoice(self):
        if self.to_invoice:
            raise UserError(_("You cannot cancel this order because its accounting entry (invoice) is not cancelled."))
        
    def action_pos_order_cancel(self):
        for rec in self:
            rec.check_moves()
            rec.check_invoice()
            session_id = rec.session_id

            coupon_lines = rec.lines.filtered(lambda l: l.coupon_id)
            for line in coupon_lines:
                if line.coupon_id.source_pos_order_id:
                    line.coupon_id.sudo().write({'source_pos_order_id': False})

            rec.sudo().write({'state': 'cancel'})

            if session_id.state == 'closed':
                cash_payments = rec.payment_ids.filtered(
                    lambda p: p.payment_method_id.l10n_mx_edi_payment_method_id.id == 1
                )
                total_cash = sum(cash_payments.mapped('amount'))

                # Localizar los asientos de pago de ESTA orden. No hay FK al
                # pos.payment, así que se ubican por partner + monto entre:
                #  - los pagos de banco (account.payment.move_id -> bank_payment_ids)
                #  - los pagos en efectivo (statement_line_ids.move_id)
                partner = rec.partner_id.commercial_partner_id
                candidate_moves = (
                    session_id.bank_payment_ids.move_id
                    | session_id.statement_line_ids.move_id
                ).filtered(lambda m: m.state == 'posted' and m.partner_id == partner)

                payment_moves = self.env['account.move']
                for pay in rec.payment_ids:
                    match = candidate_moves.filtered(
                        lambda m: m.amount_total == pay.amount and m not in payment_moves
                    )[:1]
                    payment_moves |= match

                # Cancelar los asientos de pago de ESTA orden (banco y efectivo)
                # en lugar de crear una contrapartida/reversa. Esto rompe la
                # conciliación con el cierre (comportamiento aceptado): el pago
                # queda anulado (state=cancel) y no se ven dos asientos publicados.
                # La CxC se cuadra más abajo (cierre vs reverso de la venta).
                if payment_moves:
                    to_cancel = payment_moves.filtered(lambda m: m.state == 'posted')
                    if to_cancel:
                        to_cancel.button_draft()
                        to_cancel.filtered(lambda m: m.state == 'draft').button_cancel()

                # Reverso de la venta para sacar SOLO esta orden del cierre, y solo
                # si la orden NUNCA se facturó (si tuvo factura, su venta ya salió
                # del cierre al facturar y volver a reversar la duplicaría). Ya no
                # se pasa el pago porque fue cancelado; la CxC se concilia abajo.
                if not rec.account_move:
                    rec._create_misc_reversal_move(self.env['account.move'])

                # Conciliar las partidas de CxC de esta orden que quedaron abiertas
                # (cargo del cierre + crédito de su reversa + reversa del pago), que
                # netean a cero pero no se concilian solas por el cancel=True.
                receivable = rec.config_id.property_account_receivable_id
                related_moves = (
                    session_id.move_id if session_id.move_id else self.env['account.move']
                )
                # Asientos POSS de reversa de ESTA orden (por ref con el nombre de la orden).
                reversal_moves = self.env['account.move'].search([
                    ('pos_session_id', '=', session_id.id),
                    ('ref', 'ilike', rec.name),
                    '|',
                    ('ref', 'ilike', 'Reversal'),
                    ('ref', 'ilike', 'Reversión'),
                ])
                related_moves |= reversal_moves

                cxc_open = related_moves.line_ids.filtered(
                    lambda l: l.account_id == receivable and not l.reconciled
                )
                if len(cxc_open) > 1 and abs(sum(cxc_open.mapped('balance'))) < 0.01:
                    try:
                        cxc_open.reconcile()
                    except Exception:
                        pass

                session_id.sudo().write({
                    'cash_register_balance_end': session_id.cash_register_balance_end - total_cash,
                    'cash_register_balance_end_real': session_id.cash_register_balance_end_real - total_cash,
                    'cash_register_total_entry_encoding': session_id.cash_register_total_entry_encoding - total_cash,
                })
            else:
                rec.payment_ids.unlink()

        return True
        
    """def action_pos_order_cancel(self):
        for rec in self:
            rec.check_moves()
            rec.check_invoice()
            session_id = rec.session_id

            coupon_lines = rec.lines.filtered(lambda l: l.coupon_id)
            for line in coupon_lines:
                if line.coupon_id.source_pos_order_id:
                    line.coupon_id.sudo().write({'source_pos_order_id': False})

            rec.sudo().write({'state': 'cancel'})

            if session_id.state == 'closed':
                cash_payments = rec.payment_ids.filtered(
                    lambda p: p.payment_method_id.l10n_mx_edi_payment_method_id.id == 1
                )
                total_cash = sum(cash_payments.mapped('amount'))

                # Localizar los asientos de banco (PBINMX) de ESTA orden entre los
                # bank_payment_ids de la sesión (por partner + monto): account.payment
                # -> move_id, y ese asiento trae pos_session_id lleno. No hay FK
                # directo al pos.payment.
                partner = rec.partner_id.commercial_partner_id
                non_cash = rec.payment_ids.filtered(
                    lambda p: not p.payment_method_id.is_cash_count
                )
                candidates = session_id.bank_payment_ids.move_id.filtered(
                    lambda m: m.state == 'posted' and m.partner_id == partner
                )
                payment_moves = self.env['account.move']
                for pay in non_cash:
                    match = candidates.filtered(
                        lambda m: m.amount_total == pay.amount and m not in payment_moves
                    )[:1]
                    payment_moves |= match

                # Reversar (cancelar) esos pagos. cancel=True contabiliza la reversa
                # y concilia Ingresos por Conciliar contra el original -> queda en
                # cero y el PBINMX queda marcado como revertido. Se le pone ref con
                # "Reversión" y pos_session_id para que aparezca en los apuntes de la
                # sesión (_get_related_account_moves).
                payment_reversals = self.env['account.move']
                if payment_moves:
                    payment_reversals = payment_moves._reverse_moves(
                        default_values_list=[{
                            'date': fields.Date.context_today(rec),
                            'ref': _('Reversión de pago %s') % rec.name,
                            'pos_session_id': session_id.id,
                        } for move in payment_moves],
                        cancel=True,
                    )

                # Sacar SOLO esta orden del cierre. Pasando las reversas del pago, la
                # conciliación interna del método liga el crédito de CxC del reverso
                # de la venta contra el cargo de CxC de la reversa del pago -> CxC en
                # cero. Nunca pone en borrador session.move_id ni toca las hermanas.
                rec._create_misc_reversal_move(payment_reversals or payment_moves)

                # Nota: el asiento misceláneo (POSS) y las reversas de pago NO se
                # agregan a invoice_history_ids; ese historial es exclusivamente
                # para facturas y notas de crédito.

                session_id.sudo().write({
                    'cash_register_balance_end': session_id.cash_register_balance_end - total_cash,
                    'cash_register_balance_end_real': session_id.cash_register_balance_end_real - total_cash,
                    'cash_register_total_entry_encoding': session_id.cash_register_total_entry_encoding - total_cash,
                })
            else:
                rec.payment_ids.unlink()

        return True"""
        
    """def action_pos_order_cancel(self):
        for rec in self:
            rec.check_moves()
            rec.check_invoice()
            session_id = rec.session_id

            coupon_lines = rec.lines.filtered(lambda l: l.coupon_id)
            for line in coupon_lines:
                if line.coupon_id.source_pos_order_id:
                    line.coupon_id.sudo().write({'source_pos_order_id': False})

            rec.sudo().write({'state': 'cancel'})

            if session_id.state == 'closed':
                cash_payments = rec.payment_ids.filtered(
                    lambda p: p.payment_method_id.l10n_mx_edi_payment_method_id.id == 1
                )
                total_cash = sum(cash_payments.mapped('amount'))

                payment_moves = rec.payment_ids.account_move_id.filtered(
                    lambda m: m.state == 'posted'
                )

                # Saca SOLO esta orden del cierre. Devuelve el asiento compensatorio.
                reversal_entry = rec._create_misc_reversal_move(payment_moves)

                # MATAR EL PAGO: contrapartida Dr CxC / Cr cuenta de tránsito por
                # cada pago no-efectivo, sin depender de encontrar el asiento de
                # banco (que no está ligado a la orden).
                non_cash = rec.payment_ids.filtered(
                    lambda p: not p.payment_method_id.is_cash_count
                )
                if non_cash and reversal_entry:
                    receivable = rec.config_id.property_account_receivable_id
                    partner = rec.partner_id.commercial_partner_id
                    void_lines = []
                    for pay in non_cash:
                        outstanding = (
                            pay.payment_method_id.outstanding_account_id
                            or rec.company_id.account_journal_payment_debit_account_id
                        )
                        void_lines += [
                            (0, 0, {
                                'name': _('Cancelación de pago %s') % rec.name,
                                'account_id': receivable.id,
                                'debit': pay.amount, 'credit': 0.0,
                                'partner_id': partner.id,
                            }),
                            (0, 0, {
                                'name': _('Cancelación de pago %s') % rec.name,
                                'account_id': outstanding.id,
                                'debit': 0.0, 'credit': pay.amount,
                                'partner_id': partner.id,
                            }),
                        ]
                    void_entry = self.env['account.move'].sudo().create({
                        'journal_id': rec.config_id.journal_id.id,
                        'pos_session_id': rec.session_id.id,
                        'date': fields.Date.context_today(rec),
                        'ref': _('Cancelación de pago %s') % rec.name,
                        'line_ids': void_lines,
                    })
                    void_entry.action_post()

                    # Conciliar CxC: Dr de la contrapartida contra el Cr de CxC del
                    # reverso de la venta -> la cuenta por cobrar queda en cero.
                    cxc = (reversal_entry.line_ids | void_entry.line_ids).filtered(
                        lambda l: l.account_id == receivable and not l.reconciled
                    )
                    if len(cxc) > 1:
                        try:
                            cxc.reconcile()
                        except Exception:
                            pass

                    # Conciliar la cuenta de tránsito contra el pago de banco de la
                    # sesión (localizado por sesión + partner + cuenta, ya que no
                    # hay FK directo) -> Ingresos por Conciliar queda en cero.
                    for pay in non_cash:
                        outstanding = (
                            pay.payment_method_id.outstanding_account_id
                            or rec.company_id.account_journal_payment_debit_account_id
                        )
                        bank = rec.session_id.bank_payment_ids.move_id.line_ids.filtered(
                            lambda l: l.account_id == outstanding
                            and l.partner_id == partner and not l.reconciled
                        )
                        vout = void_entry.line_ids.filtered(
                            lambda l: l.account_id == outstanding and not l.reconciled
                        )
                        if len((bank | vout)) > 1:
                            try:
                                (bank | vout).reconcile()
                            except Exception:
                                pass

                session_id.sudo().write({
                    'cash_register_balance_end': session_id.cash_register_balance_end - total_cash,
                    'cash_register_balance_end_real': session_id.cash_register_balance_end_real - total_cash,
                    'cash_register_total_entry_encoding': session_id.cash_register_total_entry_encoding - total_cash,
                })
            else:
                rec.payment_ids.unlink()

        return True"""

    """def action_pos_order_cancel(self):
        for rec in self:
            rec.check_moves()
            rec.check_invoice()
            session_id = rec.session_id

            coupon_lines = rec.lines.filtered(lambda l: l.coupon_id)
            for line in coupon_lines:
                if line.coupon_id.source_pos_order_id:
                    line.coupon_id.sudo().write({'source_pos_order_id': False})

            rec.sudo().write({'state': 'cancel'})

            if session_id.state == 'closed':
                cash_payments = rec.payment_ids.filtered(
                    lambda p: p.payment_method_id.l10n_mx_edi_payment_method_id.id == 1
                )
                total_cash = sum(cash_payments.mapped('amount'))

                # Saca SOLO esta orden del cierre. No pone en borrador
                # session.move_id ni los asientos de las hermanas, así que sus
                # pagos conciliados con sus facturas se conservan.
                payment_moves = rec.payment_ids.account_move_id.filtered(
                    lambda m: m.state == 'posted'
                )
                rec._create_misc_reversal_move(payment_moves)

                session_id.sudo().write({
                    'cash_register_balance_end': session_id.cash_register_balance_end - total_cash,
                    'cash_register_balance_end_real': session_id.cash_register_balance_end_real - total_cash,
                    'cash_register_total_entry_encoding': session_id.cash_register_total_entry_encoding - total_cash,
                })
            else:
                rec.payment_ids.unlink()

        return True"""
    
    """def action_pos_order_cancel(self):
        for rec in self:
            rec.check_moves()
            rec.check_invoice()
            session_id = rec.session_id

            coupon_lines = rec.lines.filtered(lambda l: l.coupon_id)
            for line in coupon_lines:
                if line.coupon_id.source_pos_order_id:
                    line.coupon_id.sudo().write({'source_pos_order_id': False})

            rec.sudo().write({'state': 'cancel'})

            if session_id.state == 'closed':
                cash_payments = rec.payment_ids.filtered(
                    lambda p: p.payment_method_id.l10n_mx_edi_payment_method_id.id == 1
                )
                total_cash = sum(cash_payments.mapped('amount'))

                # Saca SOLO esta orden del cierre. No pone en borrador
                # session.move_id ni los asientos de las hermanas, así que sus
                # pagos conciliados con sus facturas se conservan.
                payment_moves = rec.payment_ids.account_move_id.filtered(
                    lambda m: m.state == 'posted'
                )
                rec._create_misc_reversal_move(payment_moves)

                session_id.sudo().write({
                    'cash_register_balance_end': session_id.cash_register_balance_end - total_cash,
                    'cash_register_balance_end_real': session_id.cash_register_balance_end_real - total_cash,
                    'cash_register_total_entry_encoding': session_id.cash_register_total_entry_encoding - total_cash,
                })
            else:
                rec.payment_ids.unlink()

        return True"""

    def write(self, vals):
        res = super().write(vals)
        for record in self:
            partner_id = record.partner_id
            if partner_id:          
                partner_id.write({'property_account_receivable_id': record.config_id.property_account_receivable_id.id})
        return res

    def _pos_line_is_tax_included_priced(self, line):
        """True when the line's price_unit ALREADY includes the tax (orders
        created under the 'IVA incluido en POS' scheme). Old orders stored
        price_unit as the tax-EXCLUDED base and must keep the forward tax
        computation; otherwise invoicing/reversing them applies a backward
        computation to an excluded price and the totals break.

        Detection is data-driven: compare price_unit*qty (after discount) with
        the stored tax-included vs tax-excluded subtotals and pick the closest.
        """
        if not line:
            return False
        qty = line.qty or 1.0
        unit = line.price_unit * (1.0 - (line.discount or 0.0) / 100.0)
        base_amount = abs(unit * qty)
        dist_incl = abs(base_amount - abs(line.price_subtotal_incl))
        dist_excl = abs(base_amount - abs(line.price_subtotal))
        return dist_incl <= dist_excl

    def _pos_is_tax_included_order(self):
        """True when this order was priced tax-included (new scheme). Used to
        decide whether the 'IVA incluido en POS' backward computation applies
        when (re)invoicing; old orders must be left computing forward.
        """
        self.ensure_one()
        lines = self.lines.filtered(lambda l: l.price_subtotal_incl or l.price_subtotal)
        return bool(lines) and all(self._pos_line_is_tax_included_priced(l) for l in lines)

    def _prepare_aml_values_list_per_nature(self):
        self.ensure_one()
        sign = 1 if self.amount_total < 0 else -1
        commercial_partner = self.partner_id.commercial_partner_id
        company_currency = self.company_id.currency_id
        rate = self.currency_id._get_conversion_rate(self.currency_id, company_currency, self.company_id, self.date_order)

        # Concert each order line to a dictionary containing business values. Also, prepare for taxes computation.
        base_line_vals_list = self._prepare_tax_base_line_values(sign=-1)
        # POS "tax included" mode: for lines whose tax carries pos_price_include,
        # force the NATIVE 'force_price_include' in the base line's extra_context
        # so _compute_taxes computes the tax backwards (base = price / (1 + rate)),
        # matching the order and the invoice. Without this the reversal recomputes
        # the tax 'excluded' (forward) and the entry does not balance.
        for base_line in base_line_vals_list:
            taxes = base_line.get('taxes')
            line = base_line.get('record')
            if taxes and all(t.pos_price_include for t in taxes) and self._pos_line_is_tax_included_priced(line):
                base_line['extra_context'] = {
                    **(base_line.get('extra_context') or {}),
                    'force_price_include': True,
                }
        tax_results = self.env['account.tax']._compute_taxes(base_line_vals_list)

        total_balance = 0.0
        total_amount_currency = 0.0
        aml_vals_list_per_nature = defaultdict(list)

        # Create the tax lines
        for tax_line_vals in tax_results['tax_lines_to_add']:
            tax_rep = self.env['account.tax.repartition.line'].browse(tax_line_vals['tax_repartition_line_id'])
            amount_currency = tax_line_vals['tax_amount']
            balance = company_currency.round(amount_currency * rate)
            aml_vals_list_per_nature['tax'].append({
                'name': tax_rep.tax_id.name,
                'account_id': tax_line_vals['account_id'],
                'partner_id': tax_line_vals['partner_id'],
                'currency_id': tax_line_vals['currency_id'],
                'tax_repartition_line_id': tax_line_vals['tax_repartition_line_id'],
                'tax_ids': tax_line_vals['tax_ids'],
                'tax_tag_ids': tax_line_vals['tax_tag_ids'],
                'group_tax_id': None if tax_rep.tax_id.id == tax_line_vals['tax_id'] else tax_line_vals['tax_id'],
                'amount_currency': amount_currency,
                'balance': balance,
                'tax_tag_invert': tax_rep.document_type != 'refund',
            })
            total_amount_currency += amount_currency
            total_balance += balance

        # Create the aml values for order lines.
        for base_line_vals, update_base_line_vals in tax_results['base_lines_to_update']:
            order_line = base_line_vals['record']
            amount_currency = update_base_line_vals['price_subtotal']
            balance = company_currency.round(amount_currency * rate)
            aml_vals_list_per_nature['product'].append({
                'name': order_line.full_product_name,
                'account_id': base_line_vals['account'].id,
                'partner_id': base_line_vals['partner'].id,
                'currency_id': base_line_vals['currency'].id,
                'tax_ids': [(6, 0, base_line_vals['taxes'].ids)],
                'tax_tag_ids': update_base_line_vals['tax_tag_ids'],
                'amount_currency': amount_currency,
                'balance': balance,
                'tax_tag_invert': not base_line_vals['is_refund'],
            })
            total_amount_currency += amount_currency
            total_balance += balance

        # Cash rounding.
        cash_rounding = self.config_id.rounding_method
        if self.config_id.cash_rounding and cash_rounding and (not self.config_id.only_round_cash_method or any(p.payment_method_id.is_cash_count for p in self.payment_ids)):
            amount_currency = cash_rounding.compute_difference(self.currency_id, total_amount_currency)
            if not self.currency_id.is_zero(amount_currency):
                balance = company_currency.round(amount_currency * rate)

                if cash_rounding.strategy == 'biggest_tax':
                    biggest_tax_aml_vals = None
                    for aml_vals in aml_vals_list_per_nature['tax']:
                        if not biggest_tax_aml_vals or float_compare(-sign * aml_vals['amount_currency'], -sign * biggest_tax_aml_vals['amount_currency'], precision_rounding=self.currency_id.rounding) > 0:
                            biggest_tax_aml_vals = aml_vals
                    if biggest_tax_aml_vals:
                        biggest_tax_aml_vals['amount_currency'] += amount_currency
                        biggest_tax_aml_vals['balance'] += balance
                elif cash_rounding.strategy == 'add_invoice_line':
                    if -sign * amount_currency > 0.0 and cash_rounding.loss_account_id:
                        account_id = cash_rounding.loss_account_id.id
                    else:
                        account_id = cash_rounding.profit_account_id.id
                    aml_vals_list_per_nature['cash_rounding'].append({
                        'name': cash_rounding.name,
                        'account_id': account_id,
                        'partner_id': commercial_partner.id,
                        'currency_id': self.currency_id.id,
                        'amount_currency': amount_currency,
                        'balance': balance,
                        'display_type': 'rounding',
                    })

        # Stock.
        if self.company_id.anglo_saxon_accounting and self.picking_ids.ids:
            stock_moves = self.env['stock.move'].sudo().search([
                ('picking_id', 'in', self.picking_ids.ids),
                ('product_id.categ_id.property_valuation', '=', 'real_time')
            ])
            for stock_move in stock_moves:
                expense_account = stock_move.product_id._get_product_accounts()['expense']
                stock_output_account = stock_move.product_id.categ_id.property_stock_account_output_categ_id
                balance = -sum(stock_move.stock_valuation_layer_ids.mapped('value'))
                aml_vals_list_per_nature['stock'].append({
                    'name': _("Stock input for %s", stock_move.product_id.name),
                    'account_id': expense_account.id,
                    'partner_id': commercial_partner.id,
                    'currency_id': self.company_id.currency_id.id,
                    'amount_currency': balance,
                    'balance': balance,
                })
                aml_vals_list_per_nature['stock'].append({
                    'name': _("Stock output for %s", stock_move.product_id.name),
                    'account_id': stock_output_account.id,
                    'partner_id': commercial_partner.id,
                    'currency_id': self.company_id.currency_id.id,
                    'amount_currency': -balance,
                    'balance': -balance,
                })

        # sort self.payment_ids by is_split_transaction:
        for payment_id in self.payment_ids:
            is_split_transaction = payment_id.payment_method_id.split_transactions
            if is_split_transaction:
                reversed_move_receivable_account_id = self.partner_id.property_account_receivable_id
            else:
                reversed_move_receivable_account_id = self.config_id.property_account_receivable_id

            aml_vals_entry_found = [aml_entry for aml_entry in aml_vals_list_per_nature['payment_terms']
                                    if aml_entry['account_id'] == reversed_move_receivable_account_id.id
                                    and not aml_entry['partner_id']]

            if aml_vals_entry_found and not is_split_transaction:
                aml_vals_entry_found[0]['amount_currency'] += self.session_id._amount_converter(payment_id.amount, self.date_order, False)
                aml_vals_entry_found[0]['balance'] += payment_id.amount
            else:
                aml_vals_list_per_nature['payment_terms'].append({
                    'partner_id': commercial_partner.id if is_split_transaction else False,
                    'name': f"{reversed_move_receivable_account_id.code} {reversed_move_receivable_account_id.code}",
                    'account_id': reversed_move_receivable_account_id.id,
                    'currency_id': self.currency_id.id,
                    'amount_currency': payment_id.amount,
                    'balance': self.session_id._amount_converter(payment_id.amount, self.date_order, False),
                })

        return aml_vals_list_per_nature

    def _create_misc_reversal_move(self, payment_moves):
        """ Create a misc move to reverse this POS order and "remove" it from the POS closing entry.
        This is done by taking data from the order and using it to somewhat replicate the resulting entry in order to
        reverse partially the movements done ine the POS closing entry.
        """
        aml_values_list_per_nature = self._prepare_aml_values_list_per_nature()
        move_lines = []
        for aml_values_list in aml_values_list_per_nature.values():
            for aml_values in aml_values_list:
                aml_values['balance'] = -aml_values['balance']
                aml_values['amount_currency'] = -aml_values['amount_currency']
                move_lines.append(aml_values)

        # Make a move with all the lines.
        reversal_entry = self.env['account.move'].with_context(
            default_journal_id=self.config_id.journal_id.id,
            skip_invoice_sync=True,
            skip_invoice_line_sync=True,
        ).create({
            'journal_id': self.config_id.journal_id.id,
            'pos_session_id': self.session_id.id,
            'date': fields.Date.context_today(self),
            'ref': _('Reversal of POS closing entry %s for order %s from session %s', self.session_move_id.name, self.name, self.session_id.name),
            'invoice_line_ids': [(0, 0, aml_value) for aml_value in move_lines],
        })
        reversal_entry.action_post()
        for line in reversal_entry.line_ids:
            if line.account_id.code == '4.01.00.0000':
                account_id = self.env['account.account'].sudo().search([('code', '=', '2.01.02.0029'), ('company_id', '=', reversal_entry.company_id.id)], limit=1)
                line.write({'account_id': account_id.id})

        pos_account_receivable = self.config_id.property_account_receivable_id
        account_receivable = self.payment_ids.payment_method_id.receivable_account_id
        reversal_entry_receivable = reversal_entry.line_ids.filtered(lambda l: l.account_id in (pos_account_receivable + account_receivable))
        payment_receivable = payment_moves.line_ids.filtered(lambda l: l.account_id in (pos_account_receivable + account_receivable))
        lines_to_reconcile = defaultdict(lambda: self.env['account.move.line'])
        for line in (reversal_entry_receivable | payment_receivable):
            lines_to_reconcile[line.account_id] |= line
        for line in lines_to_reconcile.values():
            line.filtered(lambda l: not l.reconciled).reconcile()

        return reversal_entry

    def refund(self):
        today = fields.Date.context_today(self)

        for order in self:
            if order.date_order:
                order_date = fields.Date.to_date(order.date_order)

                if (order_date.month == today.month and
                    order_date.year == today.year):
                    
                    raise UserError(
                        _("You cannot refund an order from the current month.")
                    )

        return super().refund()
