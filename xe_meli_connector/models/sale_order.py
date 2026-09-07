import logging
from datetime import timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

import dateutil.parser
import pytz
import requests

_logger = logging.getLogger(__name__)

# Statuses that, if Mercado Libre reports them for an order we already
# imported, are worth a chatter message so a human goes and decides what to
# do (cancel the sale, return stock, credit note, etc.) — see
# docs/superpowers/specs/2026-08-27-meli-cancellation-notice-design.md.
MELI_STATUS_CHANGE_ALERTS = {'cancelled', 'partially_refunded', 'pending_cancel'}

# The user's own fiscal rule (confirmed 2026-08-28): compare the CFDI's
# stamping date against "today" in Monterrey/CDMX time, never naive UTC.
MELI_FISCAL_TIMEZONE = pytz.timezone('America/Monterrey')

# Confirmed with the user (2026-08-30) as XE's own existing practice for
# this exact scenario: "02 - Invoice issued with errors (no replacement)".
MELI_CFDI_CANCEL_REASON = '02'

# l10n_mx_edi.document states that mean a CFDI cancellation request was
# actually ACCEPTED — read straight from
# enterprise/l10n_mx_edi/models/account_move.py,
# _l10n_mx_edi_cfdi_invoice_try_cancel's on_success callback:
#   * 'invoice_cancel_requested': the production PAC accepted the request
#     and the SAT confirmation is still pending.
#   * 'invoice_cancel': already confirmed (PAC test environment, or
#     _fetch_and_update_sat_status got the confirmation immediately).
# Anything else means the cancellation did NOT happen — notably
# 'invoice_cancel_requested_failed' / 'invoice_cancel_failed' (the
# on_failure callback), or the original 'invoice_sent' document still
# being the newest one because try_cancel silently returned early.
# try_cancel NEVER raises on a rejection, so the outcome has to be
# re-read from the document afterwards instead of assumed.
MELI_CFDI_CANCEL_OK_STATES = ('invoice_cancel_requested', 'invoice_cancel')

# l10n_mx_edi's own "the CFDI really is stamped" states, same check
# xe_sale_self_invoice/models/sale_order.py makes after
# _l10n_mx_edi_cfdi_invoice_try_send().
MELI_CFDI_SENT_STATES = ('sent', 'global_sent')


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    meli_order_id = fields.Char(
        string='Mercado Libre Order ID', copy=False,
        help="The individual Mercado Libre order ID (the 'id' field from "
             "GET /orders/$ORDER_ID) — always set, unlike 'OC Cliente' / "
             "'Ref. Cliente' below, which show the pack ID instead when "
             "the order is part of a cart (matching Ventiapp's own "
             "convention, confirmed 2026-09-01), so those two can be "
             "shared by several sibling sale orders from the same pack. "
             "This field is what every lookup by Mercado Libre order "
             "(idempotency, the SKU-mapping retry re-fetch, claims "
             "linking) actually keys on.",
    )
    meli_pack_id = fields.Char(
        string='Mercado Libre Pack ID', copy=False,
        help="Mercado Libre pack ID, when the order is part of a cart. "
             "Used to upload the invoice via "
             "/packs/$PACK_ID/fiscal_documents.",
    )
    meli_sync_source = fields.Char(
        string='Sync Source', copy=False,
        help="Identifies that this order was created by xe_meli_connector, "
             "as opposed to legacy orders imported by Ventiapp.",
    )
    meli_last_status = fields.Char(
        string='Last Mercado Libre Status', copy=False,
        help="Last status of the order as reported by Mercado Libre. Used "
             "only to detect changes (e.g. cancellations) — never "
             "triggers any automatic action in Odoo.",
    )
    meli_order_date_created = fields.Datetime(
        string='Mercado Libre Order Created At', copy=False,
        help="date_created from the Mercado Libre order resource — when "
             "the order was originally placed, regardless of when it was "
             "paid.",
    )
    meli_order_date_closed = fields.Datetime(
        string='Mercado Libre Order Paid At', copy=False,
        help="date_closed from the Mercado Libre order resource — when "
             "the order first reached confirmed/paid and stock was "
             "discounted on Mercado Libre's side. Compare against "
             "Created At to tell a genuinely late payment (large gap) "
             "from an order that landed here for another reason.",
    )
    meli_delivery_contact_status = fields.Selection([
        ('not_applicable', 'Not Applicable'),
        ('resolved', 'Resolved'),
        ('failed', 'Failed'),
    ], string='Delivery Contact Status', default='not_applicable', copy=False,
        help="Only meaningful for custom-shipping orders (XE manages "
             "delivery itself). 'Failed' means the real delivery "
             "contact (recipient's name/phone/address) could not be "
             "resolved — the order keeps the generic Mercado Libre "
             "contact meanwhile, and the polling cron "
             "(meli.config._retry_failed_delivery_contacts) retries "
             "automatically every 30 minutes, with no attempt limit.",
    )
    meli_shipping_id = fields.Char(
        string='Mercado Libre Shipping ID', copy=False,
        help="This order's shipment id — saved for every order regardless "
             "of shipment type (2026-09-08, needed for a Google Sheets "
             "report), and also lets a later delivery-contact retry (for "
             "custom-shipping orders) skip re-fetching the whole Mercado "
             "Libre order.",
    )
    meli_buyer_id = fields.Char(
        string='Mercado Libre Buyer ID', copy=False,
        help="This order's buyer id, saved only for custom-shipping "
             "orders — same purpose as meli_shipping_id, and the same "
             "value res.partner.meli_buyer_id uses to identify the "
             "buyer's master contact.",
    )
    meli_claim_ids = fields.One2many(
        'meli.claim', 'sale_order_id', string='Mercado Libre Claims',
    )
    meli_claim_count = fields.Integer(
        string='Mercado Libre Claim Count', compute='_compute_meli_claim_count',
    )
    meli_portal_url = fields.Char(
        string='Mercado Libre Portal Link', compute='_compute_meli_portal_url',
        help="Direct link to this order's own chat/messaging thread on "
             "Mercado Libre's seller portal. Derived from the pattern "
             "confirmed working for a specific claim's thread "
             "(2026-09-07, see meli.claim.meli_portal_url) by dropping "
             "its /reclamo/$CLAIM_ID suffix — this order-level variant "
             "(no claim) hasn't itself been confirmed against a real "
             "page yet, so double check it opens the right thread once "
             "deployed. MLM-only, like the rest of this module.",
    )

    @api.depends('meli_claim_ids')
    def _compute_meli_claim_count(self):
        for order in self:
            order.meli_claim_count = len(order.meli_claim_ids)

    @api.depends('meli_order_id', 'meli_pack_id')
    def _compute_meli_portal_url(self):
        for order in self:
            # The portal always keys a pack order's messaging thread by the
            # PACK id, never the individual order id (2026-09-08, same
            # finding as meli.claim.meli_portal_url — see that field's
            # docstring for the real example that surfaced this).
            portal_id = order.meli_pack_id or order.meli_order_id
            order.meli_portal_url = (
                'https://vendedores.mercadolibre.com.mx/ventas/nueva/'
                f'mensajeria/{portal_id}'
                if portal_id else False
            )

    def action_open_meli_claims(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Mercado Libre Claims'),
            'res_model': 'meli.claim',
            'view_mode': 'tree,form',
            'domain': [('sale_order_id', '=', self.id)],
        }

    @api.model
    def _meli_import_order(self, company_id, order_id):
        """Entry point for queue_job / the webhook controller / the polling
        cron. Idempotent: safe to call more than once for the same order.
        """
        order_id = str(order_id)
        existing = self.sudo().search([('meli_order_id', '=', order_id)], limit=1)

        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for company %s."
            ) % company_id)

        order_data = config._api_get(f'/orders/{order_id}')

        if existing:
            existing._meli_flag_status_change(order_data)
            existing._meli_retry_unmapped_lines(order_data)
            return existing

        return self.sudo()._meli_create_from_order_data(config, order_data)

    @api.model
    def _meli_import_order_for_batch_line(self, company_id, order_id, line_id):
        """Entry point for queue_job when importing from an Excel batch
        (see meli.import.batch.wizard). Wraps _meli_import_order but,
        unlike the webhook/polling/single-order-wizard callers, always
        records the outcome on the batch line instead of letting a
        failure surface only in the technical Queue Jobs view — the
        batch is meant to be the one place a person needs to check, so
        this never re-raises.
        """
        line = self.env['meli.import.batch.line'].sudo().browse(line_id)
        try:
            order = self.sudo()._meli_import_order(company_id, order_id)
        except Exception as exc:
            _logger.exception(
                "Mercado Libre batch import: order %s failed.", order_id,
            )
            line.write({'status': 'error', 'message': str(exc)[:500]})
            return
        if not order:
            line.write({'status': 'not_paid'})
            return
        line.write({'status': 'imported', 'sale_order_id': order.id})

    def action_meli_retry_sku_mapping(self):
        """Manual button: re-checks meli.sku.mapping right now instead of
        waiting for the next webhook notification or polling cycle (ML
        itself won't re-notify us just because we edited our own mapping
        table — nothing changed on their side).
        """
        self.ensure_one()
        if not self.meli_order_id:
            raise UserError(_("This order doesn't have a Mercado Libre reference."))
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for this company."
            ))
        order_data = config._api_get(f'/orders/{self.meli_order_id}')
        self._meli_retry_unmapped_lines(order_data)

    def _meli_retry_unmapped_lines(self, order_data):
        """For an order still in draft because some SKUs weren't mapped at
        creation time: re-resolves the mapping, adds whatever lines can now
        be built, and confirms once nothing is missing. A no-op for any
        order that's already confirmed or wasn't created by this module.
        """
        self.ensure_one()
        if self.state != 'draft' or not self.meli_sync_source:
            return
        existing_product_ids = set(self.order_line.product_id.ids)
        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data)
        new_resolved = [
            (command, debug) for command, debug in resolved_lines
            if debug['product_id'] not in existing_product_ids
        ]
        if new_resolved:
            new_product_ids = {debug['product_id'] for _command, debug in new_resolved}
            self.write({'order_line': [command for command, _debug in new_resolved]})
            new_lines = self.order_line.filtered(lambda line: line.product_id.id in new_product_ids)
            price_debug = [debug for _command, debug in new_resolved]
            self._meli_force_line_prices(new_lines, price_debug)
        if unmapped_skus:
            self._meli_post_with_mention(
                _("Still missing a mapping for: %s.") % ', '.join(unmapped_skus)
            )
        else:
            self.action_confirm()
            if 'MLF' in (self.origin or ''):
                self._meli_auto_validate_full_pickings()
            self.message_post(body=_(
                "All Mercado Libre SKUs are now mapped — order confirmed."
            ))

    def _meli_auto_validate_full_pickings(self):
        """Full orders are fulfilled from Mercado Libre's own warehouse —
        the transfer here is bookkeeping, not something a person at XE
        needs to physically pack, so it's safe to validate automatically.
        Non-Full orders still need a person to actually pick/pack/ship,
        so those transfers are left for manual handling, for now (explicit
        user decision, 2026-08-28).

        Isolated in its own savepoint: if the transfer can't be validated
        for some reason (e.g. Odoo wants a confirmation wizard for
        insufficient stock), that failure must not roll back the sale
        order itself — it just stays pending for manual review.
        """
        self.ensure_one()
        pickings = self.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    result = picking.button_validate()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: automatic validation of "
                    "transfer %s (Full order) failed — left pending for "
                    "manual review.",
                    self.client_order_ref, picking.name,
                )
                continue
            if isinstance(result, dict):
                _logger.warning(
                    "Mercado Libre order %s: transfer %s needed extra "
                    "confirmation (e.g. insufficient stock) — left "
                    "pending for manual review.",
                    self.client_order_ref, picking.name,
                )

    def _meli_return_full_pickings(self):
        """Generates and validates a full-quantity return, by code, for
        every 'done' transfer of this order — mirrors the by-code
        stock.return.picking pattern already used in production by
        morwi/addons-morwi/account_refund_sale_stock_return
        (models/account_move.py, action_post/_prepare_return_line).

        Returns the recordset of new return transfers created (empty if
        there was nothing to return). Raises UserError if a return
        cannot be resolved to a location, or if Odoo can't auto-validate
        a return transfer (e.g. it wants a backorder/insufficient-stock
        confirmation) — the caller runs this inside a savepoint and
        falls back to manual review on any such failure.

        Idempotent by itself, whatever the caller does: Mercado Libre can
        deliver the same 'cancelled' notification more than once (and the
        polling cron can race a webhook), so any move that already has a
        return of its own is skipped rather than returned a second time.
        stock.move.returned_move_ids is the inverse of
        origin_returned_move_id, which stock.return.picking._create_returns
        sets on every return move it builds. Cancelled returns don't count
        — nothing actually came back for those, so the move is still
        returnable.
        """
        self.ensure_one()
        done_pickings = self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_id.code == 'outgoing'
        )
        new_pickings = self.env['stock.picking']
        for picking in done_pickings:
            returnable_moves = picking.move_ids.filtered(
                lambda m: m.state == 'done' and not m.returned_move_ids.filtered(
                    lambda r: r.state != 'cancel'
                )
            )
            lines = [
                (0, 0, {
                    'product_id': move.product_id.id,
                    'quantity': move.quantity,
                    'move_id': move.id,
                    'uom_id': move.product_id.uom_id.id,
                })
                for move in returnable_moves
            ]
            if not lines:
                continue
            location = picking.picking_type_id.return_picking_type_id.default_location_dest_id
            if not location:
                location = self.env['stock.location'].search([
                    '|',
                    '&', ('return_location', '=', True), ('company_id', '=', False),
                    '&', ('return_location', '=', True), ('company_id', '=', picking.company_id.id),
                ], limit=1)
            if not location:
                raise UserError(_(
                    "No return location is configured for warehouse %s."
                ) % picking.picking_type_id.warehouse_id.name)
            return_wizard = self.env['stock.return.picking'].with_context(
                active_ids=picking.ids, active_id=picking.id, active_model='stock.picking',
            ).create({
                'location_id': location.id,
                'picking_id': picking.id,
                'product_return_moves': lines,
            })
            new_picking_id, _picking_type_id = return_wizard._create_returns()
            new_picking = self.env['stock.picking'].browse(new_picking_id)
            result = new_picking.button_validate()
            if isinstance(result, dict):
                raise UserError(_(
                    "Automatic validation of the return transfer %s needs "
                    "manual confirmation (e.g. insufficient stock) — "
                    "cannot auto-cancel this order."
                ) % new_picking.name)
            new_pickings |= new_picking
        return new_pickings

    def _meli_post_with_mention(self, body, mention_partner=None):
        """Posts a chatter message with a real, visible @-mention — the
        same HTML Odoo itself generates when a person types '@Name' in
        the composer — instead of just a silent notification, so it
        reads unmistakably as "hey, you" in the chatter, not just an
        entry someone might scroll past.

        Defaults to the order's salesperson. Pass `mention_partner`
        explicitly to mention someone else instead (e.g. the configured
        returns manager for a Mercado Libre claim, in meli_claim.py).
        """
        self.ensure_one()
        partner = (
            mention_partner if mention_partner is not None
            else self.user_id.partner_id
        )
        mention = ''
        if partner:
            mention = (
                f'<a href="#" data-oe-model="res.partner" data-oe-id="{partner.id}" '
                f'class="o_mail_redirect">@{partner.name}</a> '
            )
        self.message_post(body=mention + body, partner_ids=partner.ids)

    def _meli_flag_status_change(self, order_data):
        """Called for a notification/poll about an order we already
        imported. For a Full order that just got fully cancelled, runs
        the automated cancellation sequence (stock return, sale
        cancellation, invoice resolution — see
        _meli_process_full_cancellation). For everything else (non-Full
        orders, or any other status worth a look), posts the manual
        review chatter message instead, exactly as before. See
        MELI_STATUS_CHANGE_ALERTS and
        docs/superpowers/specs/2026-08-28-meli-returns-cancellations-design.md.
        """
        self.ensure_one()
        new_status = order_data.get('status')
        if not new_status or new_status == self.meli_last_status:
            return
        self.meli_last_status = new_status
        if new_status not in MELI_STATUS_CHANGE_ALERTS:
            return

        # meli_sync_source gates the whole destructive automation to
        # orders THIS module created. A legacy Ventiapp order sitting in
        # the same Full warehouse must never be auto-returned/cancelled/
        # credit-noted: before this automation existed the same code path
        # only posted a harmless chatter message, so the gap was
        # zero-risk; now it isn't. Legacy orders keep getting the
        # manual-review message below, exactly as before.
        if new_status == 'cancelled' and self.meli_sync_source:
            # ('state', '=', 'connected') matches the convention used
            # everywhere else in this file (_meli_import_order,
            # action_meli_retry_sku_mapping): a disconnected — or
            # switched-off — connection must not authorize anything.
            config = self.env['meli.config'].sudo().search([
                ('company_id', '=', self.company_id.id),
                ('state', '=', 'connected'),
            ], limit=1)
            is_full = bool(
                config and config.warehouse_fulfillment_id
                and self.warehouse_id == config.warehouse_fulfillment_id
            )
            if is_full:
                try:
                    with self.env.cr.savepoint():
                        actions = self._meli_process_full_cancellation(config)
                except Exception:
                    _logger.exception(
                        "Mercado Libre order %s: automatic Full "
                        "cancellation failed — falling back to manual "
                        "review.", self.client_order_ref,
                    )
                else:
                    # Phase 1 (stock return + sale cancellation) succeeded
                    # inside the savepoint above. Commit it explicitly,
                    # right now, before touching invoice resolution: Odoo's
                    # own l10n_mx_edi PAC-calling code
                    # (l10n_mx_edi.document._cancel_api, used below via
                    # _meli_resolve_full_invoice) does a raw self._cr.commit()
                    # on every exit path in production — that would silently
                    # invalidate a still-open savepoint out from under us.
                    # Phase 1's success must never be put at risk by
                    # whatever Phase 2 does next, so it's durably committed
                    # here, and Phase 2 runs afterward with its own,
                    # separate error handling (no shared savepoint).
                    #
                    # Guarded by the exact same _can_commit() check
                    # l10n_mx_edi itself uses (not tools.config['test_enable']
                    # and not modules.module.current_test): under
                    # TransactionCase, self.env.cr is a REAL cursor (not the
                    # TestCursor proxy HttpCase gets — confirmed in practice,
                    # 2026-08-31, after an unguarded cr.commit() here leaked
                    # test fixtures into the real database), so an
                    # unconditional commit would permanently persist test
                    # data and corrupt the test's own savepoint-based
                    # rollback. Skipping the commit during tests is exactly
                    # as safe as it is in production: when _can_commit() is
                    # False, l10n_mx_edi's own PAC call never commits either,
                    # so there is no risk left to guard against.
                    if self.env['l10n_mx_edi.document']._can_commit():
                        self.env.cr.commit()
                    invoice_actions = []
                    invoice_failed = False
                    try:
                        invoice_actions, invoice_failed = (
                            self._meli_resolve_full_invoice()
                        )
                    except Exception:
                        # Safety net only: every EXPECTED per-invoice
                        # failure is reported through the returned
                        # `invoice_failed` flag, not by raising. What
                        # lands here is an unexpected crash — and the
                        # dangerous flavour of it is a real PostgreSQL
                        # error (e.g. a NOT NULL violation), which aborts
                        # the transaction and would make the
                        # message_post() below fail with
                        # InFailedSqlTransaction too, leaving the
                        # operator with no chatter message at all and
                        # nothing but a failed queue job. So recover the
                        # transaction FIRST, before trying to report
                        # anything.
                        self._meli_recover_aborted_transaction()
                        _logger.exception(
                            "Mercado Libre order %s: stock return and sale "
                            "cancellation succeeded, but invoice resolution "
                            "failed — needs manual review.",
                            self.client_order_ref,
                        )
                        invoice_failed = True
                    message = _(
                        "Mercado Libre reports this order was "
                        "<b>cancelled</b>. Handled automatically:"
                        "<br/>%s"
                    ) % '<br/>'.join(actions + invoice_actions)
                    if invoice_failed:
                        message += '<br/>' + _(
                            "Invoice resolution did not fully succeed and "
                            "needs manual review — the stock return and "
                            "sale cancellation above were still completed "
                            "successfully."
                        )
                    self.message_post(body=message)
                    return

        detail = order_data.get('cancel_detail') or {}
        message = _(
            "Mercado Libre reports that this order changed to status "
            "<b>%(status)s</b>."
        ) % {'status': new_status}
        if detail:
            message += '<br/>' + _(
                "Reason: %(description)s (requested by %(requested_by)s)."
            ) % {
                'description': detail.get('description') or detail.get('code') or '?',
                'requested_by': detail.get('requested_by') or '?',
            }
        message += '<br/>' + _(
            "Review manually whether the sale needs to be cancelled, "
            "stock returned, and/or a credit note issued."
        )
        self.message_post(body=message)

    def _meli_process_full_cancellation(self, config):
        """Phase 1 of the Full-order cancellation automation: stock
        return + sale cancellation only (invoice resolution is Phase 2,
        run separately by the caller — see _meli_flag_status_change).
        Full orders are fulfilled from Mercado Libre's own warehouse, so
        this can be resolved end-to-end without a human: the inventory
        never left XE's control in a way that needs physical handling.
        Returns the list of HTML action descriptions to report in the
        chatter. Raises on any failure — the caller wraps this in a
        savepoint and falls back to the manual-review message.

        Deliberately does NOT call _meli_resolve_full_invoice(): Odoo's
        l10n_mx_edi PAC-calling code does a raw cr.commit() on every exit
        path in production, which would silently invalidate this
        method's savepoint. Invoice resolution must run after this
        savepoint's commit, never inside it.
        """
        self.ensure_one()
        actions = []
        returns = self._meli_return_full_pickings()
        if returns:
            actions.append(_(
                "Stock was returned to warehouse %(warehouse)s via "
                "transfer(s) %(pickings)s."
            ) % {
                'warehouse': config.warehouse_fulfillment_id.name,
                'pickings': ', '.join(returns.mapped('name')),
            })
        if self.locked:
            # sale.group_auto_done_setting auto-locks confirmed orders for
            # some users in this database, and core action_cancel() refuses
            # to touch a locked order — found in practice (2026-08-30).
            # action_unlock() is xe_pacific's override, which still raises
            # if a picking is in 'transit' status; that's a real problem
            # worth surfacing (falls back to manual review), not something
            # to force past.
            self.action_unlock()
        self.with_context(disable_cancel_warning=True).action_cancel()
        actions.append(_("The sale order was cancelled."))
        return actions

    def _meli_invoice_stamped_same_month_as_today(self, invoice):
        """Compares the CFDI's stamping date — or, if it was never
        stamped, the invoice's own accounting date — against today.

        The two branches deliberately use different notions of "today":
        `l10n_mx_edi_post_time` is NOT UTC — despite its name and despite
        looking like an ordinary naive `Datetime`, it is already
        Mexico-local wall-clock time. `enterprise/l10n_mx_edi/models/
        account_move.py` sets it via
        `_l10n_mx_edi_get_datetime_now_with_mx_timezone(...)`, which
        returns `datetime.now(tz)` with `tz` already the Mexico-City
        timezone, and then stores it through `fields.Datetime.to_string`,
        which just `strftime`s the value and discards the tzinfo (see
        `odoo/fields.py`). So the naive value that ends up on the field is
        Mexico-local, not UTC — the field's own `help` text confirms this
        ("Keep empty to use the current México central time."). Because of
        that, this branch takes `.date()` directly with no further
        timezone conversion — re-localizing it as UTC and shifting it by
        six more hours (as an earlier version of this method did) pushed
        early-morning Monterrey stamps on the 1st of the month back into
        the previous month, wrongly triggering a credit note instead of a
        CFDI cancellation. `today_local` for this branch is still derived
        from real UTC `fields.Datetime.now()` converted to Monterrey/CDMX,
        because that value genuinely is UTC.
        `invoice_date` is a plain Date with no time-of-day component —
        `action_post()` fills it with `fields.Date.context_today(self)`
        when unset, which itself defaults to UTC in the absence of a
        user/company timezone. Comparing that value against a
        Monterrey-shifted "today" is an apples-to-oranges mismatch: for
        part of every day (whenever UTC has already rolled over to a new
        month but Monterrey, six hours behind, hasn't), a same-day
        invoice would be misread as "a previous month" and get an
        unwarranted credit note instead of a CFDI cancellation.
        Confirmed in practice (2026-08-31/09-01): a test invoice posted
        moments before the comparison was judged as belonging to a
        previous month for exactly this reason. Comparing invoice_date
        against `context_today()` on the same record keeps both sides on
        the same footing, whatever timezone that resolves to.
        """
        self.ensure_one()
        if invoice.l10n_mx_edi_post_time:
            stamp_local = invoice.l10n_mx_edi_post_time.date()
            today_local = pytz.utc.localize(
                fields.Datetime.now()
            ).astimezone(MELI_FISCAL_TIMEZONE).date()
        elif invoice.invoice_date:
            stamp_local = invoice.invoice_date
            today_local = fields.Date.context_today(invoice)
        else:
            return False
        return (stamp_local.year, stamp_local.month) == (today_local.year, today_local.month)

    def _meli_transaction_is_aborted(self):
        """True when the current PostgreSQL transaction is in an aborted
        state — i.e. a real database error was raised, not just a plain
        Python exception. Probed with a harmless query rather than by
        poking at psycopg2 internals, which aren't reachable the same way
        on the real cursor and on the TestCursor.
        """
        try:
            self.env.cr.execute('SELECT 1', log_exceptions=False)
        except Exception:
            return True
        return False

    def _meli_recover_aborted_transaction(self):
        """After a real PostgreSQL error the transaction is aborted, and
        every later query — including the chatter message the operator
        still needs — fails with InFailedSqlTransaction. Rolling back is
        what makes reporting possible again.

        Guarded by the exact same _can_commit() check as the Phase 1
        commit, for the same reason: under tests self.env.cr is a REAL
        cursor, so an unconditional rollback would throw away the test's
        own fixtures — and there is nothing to recover from anyway,
        because l10n_mx_edi's PAC code doesn't commit under tests either.
        """
        if self.env['l10n_mx_edi.document']._can_commit():
            self.env.cr.rollback()

    def _meli_resolve_full_invoice(self):
        """For every posted customer invoice on this order: cancel the
        CFDI if it was stamped this calendar month (Monterrey time),
        otherwise issue AND stamp a full credit note.

        Returns ``(actions, any_failed)``: the list of HTML action
        descriptions to report in the chatter — one per invoice, for
        failures just as much as for successes, so nothing is ever
        silently dropped — plus a boolean telling the caller whether at
        least one invoice could not be resolved.

        Per-invoice failures are reported through that boolean, never by
        raising. One invoice's failure must not hide what already
        happened to the others: a CFDI cancellation is irreversible and
        l10n_mx_edi commits it on the spot, so its action text has to
        reach the chatter whatever a later invoice does.

        The one thing that still propagates is a real database error,
        which aborts the whole transaction and makes any further work —
        reporting included — impossible until something rolls back;
        Phase 2's safety net in _meli_flag_status_change handles that.

        Called from Phase 2, deliberately outside any savepoint, since
        l10n_mx_edi's PAC calls force their own commit that a savepoint
        here would only get corrupted by.
        """
        self.ensure_one()
        actions = []
        any_failed = False
        # move_type is filtered explicitly: self.invoice_ids also holds
        # any credit note already sitting on this order (partial refunds
        # are still handled manually today), and reversing a credit note
        # would issue a brand-new invoice to the customer.
        invoices = self.invoice_ids.filtered(
            lambda m: m.state == 'posted' and m.move_type == 'out_invoice'
        )
        for invoice in invoices:
            try:
                if self._meli_invoice_stamped_same_month_as_today(invoice):
                    action, failed = self._meli_cancel_invoice_cfdi(invoice)
                else:
                    action, failed = self._meli_credit_note_for_invoice(invoice)
            except Exception:
                if self._meli_transaction_is_aborted():
                    # Nothing can be recorded or reported until the
                    # transaction is rolled back — hand it to Phase 2's
                    # safety net, which does exactly that first.
                    raise
                _logger.exception(
                    "Mercado Libre order %s: unexpected error while "
                    "resolving invoice %s — reported for manual review.",
                    self.client_order_ref, invoice.name,
                )
                action = _(
                    "Invoice %s could not be resolved automatically "
                    "(unexpected error) — review manually."
                ) % invoice.name
                failed = True
            actions.append(action)
            any_failed = any_failed or failed
        return actions, any_failed

    def _meli_cancel_invoice_cfdi(self, invoice):
        """Same-month branch: request the CFDI cancellation with the SAT.
        Returns ``(html_action_description, failed)``.
        """
        self.ensure_one()
        document = invoice.l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_sent'
        )[:1]
        if not document:
            # Reported instead of silently skipped: an invoice that's
            # posted but carries no live CFDI document is precisely the
            # case a human has to look at.
            return _(
                "Invoice %s was stamped this month, but it has no active "
                "CFDI document to cancel — review manually."
            ) % invoice.name, True
        invoice._l10n_mx_edi_cfdi_invoice_try_cancel(
            document, MELI_CFDI_CANCEL_REASON,
        )
        self.env['l10n_mx_edi.document']._fetch_and_update_sat_status(
            extra_domain=[('id', '=', document.id)],
        )
        # _l10n_mx_edi_cfdi_invoice_try_cancel never raises when the PAC
        # or the SAT rejects the request — it records the outcome on an
        # l10n_mx_edi.document and returns normally. The newest document
        # (the model is ordered 'datetime DESC, id DESC') is therefore
        # the only honest source of truth about what actually happened;
        # `document` itself stays in 'invoice_sent' either way.
        latest = invoice.l10n_mx_edi_invoice_document_ids.sorted()[:1]
        if latest.state in MELI_CFDI_CANCEL_OK_STATES:
            return _(
                "CFDI cancellation was requested for invoice %s "
                "(stamped this month)."
            ) % invoice.name, False
        return _(
            "The CFDI cancellation for invoice %(invoice)s (stamped this "
            "month) was REJECTED: %(error)s. Review manually."
        ) % {
            'invoice': invoice.name,
            'error': latest.message or _("no error message was recorded"),
        }, True

    def _meli_credit_note_for_invoice(self, invoice):
        """Previous-month branch: issue a full credit note, post it, and
        stamp it with the SAT. Returns
        ``(html_action_description, failed)``.
        """
        self.ensure_one()
        # move_ids/journal_id/company_id are set explicitly here rather
        # than left to the wizard's own default_get() /
        # _compute_journal_id (as account_move_reversal.py's normal flow
        # does, driven by active_model/active_ids context): that
        # resolution chain only completes because the web client runs an
        # onchange pass client-side before the create() RPC is even sent.
        # Calling create() headlessly like this skips that pass —
        # confirmed in practice (2026-08-31): journal_id (required,
        # stored, computed from move_ids) was still unset by the time of
        # the INSERT, since move_ids (a many2many) is only linked to the
        # record after the initial row already exists, violating the
        # column's NOT NULL constraint. Once journal_id was set
        # explicitly, company_id defaulted to env.company instead of the
        # invoice's own company (default_get's context-driven resolution
        # was no longer in play), which then failed _check_company().
        # Setting all three explicitly sidesteps the ordering issue
        # entirely, using the exact same values the wizard's own
        # defaults/computes would have produced for a single-invoice
        # reversal — including mirroring _compute_journal_id's own
        # active-journal filter (account_move_reversal.py), so an
        # archived journal on the invoice is left unset here too, exactly
        # like core would, instead of silently posting the credit note
        # against an archived journal.
        journal = invoice.journal_id.filtered('active')
        wizard = self.env['account.move.reversal'].create({
            'move_ids': [(6, 0, invoice.ids)],
            'journal_id': journal.id,
            'company_id': invoice.company_id.id,
            'reason': _(
                "Automatic reversal — Mercado Libre order %s "
                "was cancelled outside the invoice's issuance "
                "month."
            ) % self.client_order_ref,
        })
        wizard.reverse_moves(is_modify=False)
        credit_note = wizard.new_move_ids
        # The wizard leaves the credit note in draft — confirmed with the
        # user (2026-08-31) that Full-order automation must post (and
        # thus stamp) it immediately, not leave a manual-review
        # checkpoint.
        credit_note.action_post()
        # action_post() does NOT stamp anything: l10n_mx_edi's _post()
        # override only records l10n_mx_edi_post_time. The actual PAC
        # call is a separate step — the same two-call pattern already
        # used in this repo by xe_sale_self_invoice
        # (models/sale_order.py) and xe_customs
        # (wizard/account_move_send.py). Without it the credit note would
        # exist in Odoo with no CFDI at the SAT: the original invoice
        # stays fiscally valid and the customer's refund is never
        # declared — exactly the hole this automation exists to close.
        credit_note._l10n_mx_edi_cfdi_invoice_try_send()
        if credit_note.l10n_mx_edi_cfdi_state in MELI_CFDI_SENT_STATES:
            return _(
                "A full credit note (%(credit_note)s) was issued and "
                "stamped for invoice %(invoice)s (stamped in a previous "
                "month)."
            ) % {
                'credit_note': credit_note.name,
                'invoice': invoice.name,
            }, False
        # try_send doesn't raise on a PAC rejection either; it stores the
        # reason on the document, same as the cancellation flow.
        failed_document = credit_note.l10n_mx_edi_invoice_document_ids.sorted()[:1]
        return _(
            "A full credit note (%(credit_note)s) was issued for invoice "
            "%(invoice)s (stamped in a previous month), but its CFDI "
            "could NOT be stamped: %(error)s. The credit note is posted "
            "in Odoo without a valid CFDI — review manually."
        ) % {
            'credit_note': credit_note.name,
            'invoice': invoice.name,
            'error': failed_document.message or _("no error message was recorded"),
        }, True

    @api.model
    def _meli_create_from_order_data(self, config, order_data):
        order_id = str(order_data.get('id'))
        existing = self.search([('meli_order_id', '=', order_id)], limit=1)
        if existing:
            return existing

        if order_data.get('status') != 'paid':
            _logger.info(
                "Mercado Libre order %s has status '%s' (not 'paid' yet), "
                "skipping import.", order_id, order_data.get('status'),
            )
            return self.browse()

        if not config.partner_id:
            raise UserError(_(
                "Configure the 'Mercado Libre Customer' on the connection "
                "(Mercado Libre > Settings) before importing sales."
            ))

        logistic_type = self._meli_fetch_logistic_type(config, order_id, order_data)
        is_fulfillment = logistic_type == 'fulfillment'
        warehouse = (
            config.warehouse_fulfillment_id if is_fulfillment
            else config.warehouse_default_id
        )
        if not warehouse:
            raise UserError(_(
                "Configure '%(field)s' on the Mercado Libre connection "
                "(Mercado Libre > Settings) before importing sales."
            ) % {
                'field': _('Fulfillment Warehouse (Full)') if is_fulfillment
                         else _('Default Warehouse (non-Full)'),
            })
        tag = 'MLF' if is_fulfillment else 'ML'

        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data)

        # A transient failure here must never be confused with "this
        # order isn't custom shipping" (both used to read as None) — that
        # silently dropped the freight surcharge with nothing to catch it
        # (_meli_retry_unmapped_lines never revisits shipping once the
        # order exists). shipping_cost_fetch_failed lets the order still
        # get created without the surcharge, exactly as before, but with
        # a chatter warning posted below once the order exists.
        shipping_cost_fetch_failed = False
        try:
            custom_shipping_cost = self._meli_fetch_custom_shipping_cost(
                config, order_id, order_data,
            )
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not verify the Mercado Libre shipping mode for "
                "order %s — creating the order without a shipping "
                "surcharge line.", order_id,
            )
            custom_shipping_cost = None
            shipping_cost_fetch_failed = True
        shipping_partner_id = config.partner_id.id
        delivery_contact_status = 'not_applicable'
        # Read regardless of shipment type (2026-09-08, for a Google
        # Sheets report keyed on it) — the order resource always carries
        # its own shipping.id, Full/fulfillment orders included; only
        # meli_buyer_id and the delivery-contact resolution below stay
        # scoped to custom shipping, since that's their only real use.
        shipping_id = (order_data.get('shipping') or {}).get('id')
        meli_shipping_id = str(shipping_id) if shipping_id else False
        meli_buyer_id = False
        if custom_shipping_cost is not None:
            if not config.shipping_item_id:
                raise UserError(_(
                    "Configure 'Shipping Item' on the Mercado Libre "
                    "connection (Mercado Libre > Settings) before "
                    "importing an order with custom shipping (Mercado "
                    "Libre order %s)."
                ) % order_id)
            shipping_price_unit = self._meli_price_unit_untaxed(
                config.shipping_item_id, custom_shipping_cost,
            )
            resolved_lines.append((
                (0, 0, {
                    'product_id': config.shipping_item_id.id,
                    'product_uom_qty': 1,
                    'price_unit': shipping_price_unit,
                }),
                {
                    'sku': _('Custom shipping'),
                    'product_id': config.shipping_item_id.id,
                    'ml_unit_price': custom_shipping_cost,
                    'price_unit': shipping_price_unit,
                },
            ))
            buyer_id = str((order_data.get('buyer') or {}).get('id') or '')
            meli_buyer_id = buyer_id or False
            destination = self._meli_fetch_custom_shipping_destination(
                config, shipping_id,
            )
            delivery_contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
                buyer_id, destination,
            )
            if delivery_contact:
                shipping_partner_id = delivery_contact.id
                delivery_contact_status = 'resolved'
            else:
                delivery_contact_status = 'failed'
        # 'custom' is passed to _meli_build_note instead of the raw
        # logistic_type whenever we know the shipment IS custom shipping:
        # _meli_fetch_logistic_type returns None for a custom shipment (no
        # "logistic_type" key at all in that payload — see
        # _meli_fetch_custom_shipping_cost's docstring), which would
        # otherwise render as the same "N/A" a truly unknown shipment
        # gets. Falls back to logistic_type (still None/"N/A" as before)
        # when the fetch failed or the shipment genuinely isn't custom.
        note_logistics = 'custom' if custom_shipping_cost is not None else logistic_type

        line_vals = [command for command, _debug in resolved_lines]

        # Matches Ventiapp's own convention (confirmed with the user
        # 2026-09-01): when the order is part of a Mercado Libre pack
        # (cart), "OC Cliente"/"Ref. Cliente" show the pack ID instead of
        # this specific order's own ID — several sibling sale orders from
        # the same pack can end up sharing this value. meli_order_id
        # above always holds the real, unique, individual order ID and is
        # what every lookup (idempotency, SKU-mapping retry, claims) keys
        # on instead.
        pack_id = str(order_data.get('pack_id') or '') or False
        customer_ref = pack_id or order_id

        vals = {
            'company_id': config.company_id.id,
            'partner_id': config.partner_id.id,
            'partner_shipping_id': shipping_partner_id,
            'partner_invoice_id': config.partner_id.id,
            'meli_delivery_contact_status': delivery_contact_status,
            'meli_shipping_id': meli_shipping_id,
            'meli_buyer_id': meli_buyer_id,
            'client_order_ref': customer_ref,
            'reference': customer_ref,
            'origin': f'XE-{tag}-XEBRANDS',
            'meli_sync_source': 'xe_meli_connector',
            'meli_order_id': order_id,
            'meli_pack_id': pack_id,
            'meli_last_status': order_data.get('status'),
            'meli_order_date_created': self._meli_parse_datetime(order_data.get('date_created')),
            'meli_order_date_closed': self._meli_parse_datetime(order_data.get('date_closed')),
            # date_closed (when Mercado Libre closed the order, which
            # coincides with payment confirmation) rather than
            # date_created — orders are only ever imported once
            # status='paid', so what matters for sales reporting is
            # when the money was secured, not when the cart was
            # started. With deferred payment methods (OXXO, transfer)
            # date_created can be days earlier and would misdate the
            # sale into the wrong accounting period. Decided with the
            # user 2026-09-08.
            'date_order': (
                self._meli_parse_datetime(order_data.get('date_closed'))
                or fields.Datetime.now()
            ),
            'team_id': config.sale_team_id.id or False,
            'user_id': config.salesperson_id.id or False,
            'warehouse_id': warehouse.id,
            'note': self._meli_build_note(note_logistics),
            'order_line': line_vals,
        }
        order = self.create(vals)
        if shipping_cost_fetch_failed:
            # Independent of whatever else happens below (unmapped SKUs,
            # auto-confirm, auto-cancel) — this is its own, separate
            # warning that the freight surcharge may be missing.
            order._meli_post_with_mention(_(
                "Could not verify the Mercado Libre shipping mode for "
                "this order; if it's a custom shipment the freight "
                "surcharge line is missing."
            ))
        price_debug = [debug for _command, debug in resolved_lines]
        if price_debug:
            self._meli_force_line_prices(order.order_line, price_debug)
        if unmapped_skus:
            order._meli_post_with_mention(
                _(
                    "No Mercado Libre SKU mapping found for: %s. Add the "
                    "mapping under Mercado Libre > SKU Mapping and confirm "
                    "this order manually."
                ) % ', '.join(unmapped_skus)
            )
        elif order.state == 'draft':
            # Some automation in this database can react to the creation
            # itself and move the order out of draft before we get here
            # (seen in practice 2026-08-28) — action_confirm() raises a
            # UserError on anything not draft/sent, so check first instead
            # of letting the job fail for an order that's arguably already
            # in the state we wanted.
            order.action_confirm()
            if is_fulfillment:
                order._meli_auto_validate_full_pickings()
        elif order.state == 'cancel':
            order._meli_post_with_mention(
                _(
                    "The order was created, but something in this "
                    "database cancelled it automatically before we could "
                    "confirm it. Please review manually."
                )
            )
        if delivery_contact_status == 'failed':
            # Posted last, deliberately: action_confirm() above writes
            # sale.order.state (tracking=3), which posts its own
            # automatic tracking message — if this went earlier, that
            # write would bury it under a message with no @-mention.
            # mail.message is ordered 'id desc', so whatever posts last
            # is message_ids[0].
            manager_partner = (
                config.delivery_contact_manager_id.partner_id
                if config.delivery_contact_manager_id else None
            )
            order._meli_post_with_mention(_(
                "Could not resolve a real delivery contact for this "
                "custom-shipping order — using the generic Mercado "
                "Libre contact for now. This is retried automatically "
                "every 30 minutes; no further action needed unless it "
                "keeps failing."
            ), mention_partner=manager_partner)
        return order

    @api.model
    def _meli_parse_datetime(self, value):
        """Mercado Libre sends ISO8601 timestamps with their own UTC
        offset (e.g. "2013-05-27T10:01:50.000-04:00") — parse and convert
        to naive UTC, since Odoo's Datetime fields are always naive UTC
        internally. Odoo's own tools.parse_date isn't used here because it
        can't handle non-zero UTC offsets.
        """
        if not value:
            return False
        parsed = dateutil.parser.isoparse(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @api.model
    def _meli_fetch_logistic_type(self, config, order_id, order_data):
        """The order resource only carries a shipping id, not the
        logistic_type — a separate call to /orders/$ID/shipments is
        required. Falls back to None (non-fulfillment) on any failure so a
        transient issue here doesn't block the whole sale from being
        created.
        """
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return None
        try:
            shipments = config._api_get(
                f'/orders/{order_id}/shipments',
                headers={'X-New-Domain': 'true'},
            )
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch shipments for Mercado Libre order %s, "
                "defaulting to the non-fulfillment warehouse.", order_id,
            )
            return None
        if isinstance(shipments, dict):
            shipments = [shipments]
        for shipment in shipments or []:
            if shipment.get('type') == 'forward':
                return shipment.get('logistic_type')
        return None

    @api.model
    def _meli_fetch_custom_shipping_cost(self, config, order_id, order_data):
        """Mercado Libre orders shipped 'custom' (the seller manages
        courier/logistics directly — used today only for XE's oversized
        security doors, which don't fit Mercado Envíos' standard
        network) carry their own freight cost that XE must recover from
        the buyer. Returns the RAW (tax-included) cost Mercado Libre
        reports for that shipment, or None if this order's shipment
        isn't 'custom' (the vast majority of orders) or has no shipment
        at all.

        Real payload confirmed 2026-08-31 against Mercado Libre order
        2000018198314102: GET /orders/{id}/shipments (X-New-Domain:
        true — the same resource _meli_fetch_logistic_type already
        reads) returned "mode": "custom", "base_cost": 900, with no
        "logistic_type" key at all (that key only appears for
        Full/ME-managed shipments, which is why
        _meli_fetch_logistic_type needs no change here — it already
        returns None for a shipment with no such key).

        Deliberately a separate fetch from _meli_fetch_logistic_type
        (not merged into one call) — see Global Constraints in
        docs/superpowers/plans/2026-08-31-meli-custom-shipping-surcharge.md.

        Unlike _meli_fetch_logistic_type, a RequestException here is
        deliberately NOT swallowed — it propagates to the caller. This
        method's None return value already means something specific ("not
        custom shipping"), and silently reusing it for "the fetch failed"
        made a failed fetch indistinguishable from a genuinely
        non-custom order: the order got created and confirmed with no
        surcharge line and nothing looked wrong (found in the final
        branch review, 2026-08-31). _meli_create_from_order_data is the
        one that decides what a failure here should mean.
        """
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return None
        shipments = config._api_get(
            f'/orders/{order_id}/shipments',
            headers={'X-New-Domain': 'true'},
        )
        if isinstance(shipments, dict):
            shipments = [shipments]
        for shipment in shipments or []:
            if shipment.get('type') == 'forward' and shipment.get('mode') == 'custom':
                return shipment.get('base_cost') or 0.0
        return None

    @api.model
    def _meli_fetch_custom_shipping_destination(self, config, shipping_id):
        """Only meaningful for orders already confirmed as custom shipping
        (see _meli_fetch_custom_shipping_cost) — fetches the actual
        delivery recipient's name, phone and address.

        This is a DIFFERENT endpoint than _meli_fetch_logistic_type/
        _meli_fetch_custom_shipping_cost use: those read
        /orders/{id}/shipments (header X-New-Domain: true), which never
        carries the recipient. This reads /shipments/{shipping_id}
        directly (header x-format-new: true — note the different header
        name), confirmed against the official Mercado Libre
        documentation 2026-09-01. See
        docs/superpowers/specs/2026-09-01-meli-delivery-contact-design.md.

        Returns None on any fetch failure or a missing/blank
        receiver_name (the one field with no sane fallback) — this is a
        delivery-contact enrichment, not a financial figure like the
        shipping surcharge, so a failure here must never block order
        creation. Returns a flat dict otherwise; see the caller
        (_meli_create_from_order_data, via
        res.partner._meli_find_or_create_delivery_contact) for how each
        key is used.
        """
        try:
            shipment = config._api_get(
                f'/shipments/{shipping_id}',
                headers={'x-format-new': 'true'},
            )
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch shipment %s destination details for a "
                "custom-shipping order — creating it with the generic "
                "delivery contact instead.", shipping_id,
            )
            return None
        destination = (shipment or {}).get('destination') or {}
        name = (destination.get('receiver_name') or '').strip()
        if not name:
            return None
        address = destination.get('shipping_address') or {}
        street = ' '.join(filter(None, [
            (address.get('street_name') or '').strip(),
            (address.get('street_number') or '').strip(),
        ])).strip()
        state = address.get('state') or {}
        country = address.get('country') or {}
        return {
            'name': name,
            'phone': (destination.get('receiver_phone') or '').strip(),
            'street': street,
            'city': ((address.get('city') or {}).get('name') or '').strip(),
            'zip': (address.get('zip_code') or '').strip(),
            'state_name': (state.get('name') or '').strip(),
            'state_code': (state.get('id') or '').strip(),
            'country_code': (country.get('id') or '').strip(),
        }

    def _meli_retry_delivery_contact(self, config):
        """Called by meli.config._retry_failed_delivery_contacts (the
        existing 30-minute polling cron) for every order still stuck in
        meli_delivery_contact_status == 'failed'. A no-op for anything
        else — in particular, safe to call on any order regardless of
        status, since only 'failed' orders ever have meli_shipping_id
        set in the first place.

        Deliberately posts a chatter message ONLY on success (confirming
        the fix to whoever got the original failure notice) — NEVER
        re-posts the original failure warning on a retry that's still
        failing, or every 30-minute cron tick while something stays
        broken would spam the chatter/email. See
        docs/superpowers/specs/2026-09-02-meli-delivery-contact-retry-design.md.
        """
        self.ensure_one()
        if self.meli_delivery_contact_status != 'failed' or not self.meli_shipping_id:
            return
        destination = self._meli_fetch_custom_shipping_destination(
            config, self.meli_shipping_id,
        )
        delivery_contact = self.env['res.partner']._meli_find_or_create_delivery_contact(
            self.meli_buyer_id, destination,
        )
        if not delivery_contact:
            return
        self.write({
            'partner_shipping_id': delivery_contact.id,
            'meli_delivery_contact_status': 'resolved',
        })
        self.message_post(body=_(
            "Delivery contact resolved on retry: %s."
        ) % delivery_contact.name)

    @api.model
    def _meli_build_order_lines(self, order_data):
        """Returns (resolved_lines, unmapped_skus). Each entry in
        resolved_lines is (create_command, debug_dict) — the debug_dict
        carries the raw Mercado Libre price alongside the computed
        price_unit, so callers can force the real price back onto the
        line after creation (see _meli_force_line_prices).
        """
        resolved_lines = []
        unmapped_skus = []
        for order_item in order_data.get('order_items') or []:
            item = order_item.get('item') or {}
            sku = (item.get('seller_sku') or '').strip()
            product = self.env['meli.sku.mapping']._resolve_product_by_meli_sku(sku)
            if not product:
                unmapped_skus.append(sku or item.get('id') or '?')
                continue
            # Always the price Mercado Libre reports for this item — never
            # Odoo's own catalog list_price. Falling back to list_price
            # silently produced wrong totals in practice (2026-08-28):
            # whatever XE happens to have set as the catalog price has no
            # relationship to what was actually sold on ML.
            ml_unit_price = order_item.get('unit_price') or 0.0
            price_unit = self._meli_price_unit_untaxed(product, ml_unit_price)
            resolved_lines.append((
                (0, 0, {
                    'product_id': product.id,
                    'product_uom_qty': order_item.get('quantity') or 1,
                    'price_unit': price_unit,
                }),
                {
                    'sku': sku or item.get('id') or '?',
                    'product_id': product.id,
                    'ml_unit_price': ml_unit_price,
                    'price_unit': price_unit,
                },
            ))
        return resolved_lines, unmapped_skus

    @api.model
    def _meli_force_line_prices(self, lines, price_debug):
        """sale.order.line.price_unit is a precompute field driven by
        product_id/product_uom/product_uom_qty (see Odoo's
        sale/models/sale_order_line.py _compute_price_unit) — Odoo can
        silently recompute it from the pricelist right after we set it,
        discarding the real Mercado Libre price (found in practice
        2026-08-28: orders ended up priced from Odoo's own product
        list_price instead of what was actually sold). Force it back
        explicitly, matching lines to their resolved price by position —
        `lines` and `price_debug` must be in the same order they were
        created/added in.
        """
        for line, debug in zip(lines, price_debug):
            if line.price_unit != debug['price_unit']:
                line.price_unit = debug['price_unit']

    # Standard Mexican IVA rate. Every real reference order checked against
    # Ventiapp so far (2026-08-28) reduces to exactly unit_price / 1.16 —
    # this business's catalog (security/hardware products) doesn't carry
    # reduced/zero-rate IVA items.
    MELI_MX_IVA_RATE = 0.16

    @api.model
    def _meli_price_unit_untaxed(self, product, ml_unit_price):
        """Mercado Libre's unit_price is what the buyer actually paid —
        in Mexico that's always IVA-included. Our products' taxes are
        configured tax-excluded (price_unit is read as the pre-tax base),
        so passing ML's price straight through makes Odoo add IVA a
        SECOND time on top, inflating the order total.

        Divides by the standard rate directly instead of routing through
        Odoo's generic compute_all()/force_price_include tax engine: that
        approach returned wrong results for at least some products
        (2026-08-28 — off by a consistent, non-16% factor, most likely
        because of a second tax or a fiscal position on those products'
        tax setup). A plain division is simpler, predictable, and matches
        every real Ventiapp reference order verified so far exactly.
        """
        return ml_unit_price / (1 + self.MELI_MX_IVA_RATE)

    @api.model
    def _meli_build_note(self, logistic_type):
        return (
            f"<span><strong>{_('Channel')}:</strong> Mercado Libre</span><br>"
            f"<span><strong>{_('Logistics')}:</strong> {logistic_type or 'N/A'}</span>"
            "<br><br>"
            '<blockquote style="font-size: 18px;">'
            f"<strong>{_('Created by xe_meli_connector')}</strong></blockquote>"
        )
