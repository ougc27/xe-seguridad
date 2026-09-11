import base64
import logging
from datetime import timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_is_zero

import dateutil.parser
import pytz
import requests

from odoo.addons.queue_job.exception import RetryableJobError

_logger = logging.getLogger(__name__)

# Statuses that, if Mercado Libre reports them for an order we already
# imported, are worth a chatter message so a human goes and decides what to
# do (cancel the sale, return stock, credit note, etc.) — see
# docs/superpowers/specs/2026-08-27-meli-cancellation-notice-design.md.
MELI_STATUS_CHANGE_ALERTS = {'cancelled', 'partially_refunded', 'pending_cancel'}

# The user's own fiscal rule (confirmed 2026-08-28): compare the CFDI's
# stamping date against "today" in Monterrey/CDMX time, never naive UTC.
MELI_FISCAL_TIMEZONE = pytz.timezone('America/Monterrey')

# l10n_mx_edi.document.cancellation_reason is a Selection (SAT's own 4
# reason codes, see CANCELLATION_REASON_SELECTION in enterprise
# l10n_mx_edi/models/l10n_mx_edi_document.py), NOT free text — a plain
# sentence there raises ValueError (confirmed in practice). Refacturación
# always creates a replacement invoice in the very same reconciler call,
# so '01' ("Invoice issued with errors, WITH replacement") is the code
# that matches what's happening.
MELI_REFACTURA_CANCEL_REASON = '01'


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
    meli_adopted = fields.Boolean(
        string='Adopted From Another System', default=False, copy=False,
        help="Set when this order was NOT created by xe_meli_connector, "
             "but was instead a pre-existing sale.order (typically from "
             "Ventiapp, this connector's predecessor) that got LINKED to "
             "a real Mercado Libre order/pack — see the adoption branch "
             "of _meli_create_from_order_data. Several automated paths in "
             "this module (e.g. _meli_retry_unmapped_lines, the polling "
             "cron's re-enqueue of draft orders) deliberately skip an "
             "adopted order: the user's explicit instruction is that "
             "adoption may only ever update this connector's own control "
             "fields (meli_sync_source, meli_order_id, etc.) and must "
             "never go on to confirm, auto-validate a delivery, or "
             "otherwise automate anything else about an order this "
             "connector did not itself create.",
    )
    meli_last_status = fields.Char(
        string='Last Mercado Libre Status', copy=False,
        help="Last status of the order as reported by Mercado Libre. Used "
             "only to detect changes (e.g. cancellations) — never "
             "triggers any automatic action in Odoo.",
    )
    meli_has_unapplied_document = fields.Boolean(
        string='Has an Unapplied Mercado Libre Document',
        compute='_compute_meli_has_unapplied_document',
        help="True when at least one meli.invoice.document already "
             "related to this sale (sale_order_id set) was never "
             "actually applied (is_applied still False — see that "
             "field's own help text). Only ever used to hide the "
             "'Retry Invoicing Reconciliation' button once there's "
             "genuinely nothing left for it to do.",
    )

    @api.depends()
    def _compute_meli_has_unapplied_document(self):
        # Not stored on purpose — this only ever gates a button's
        # visibility on a freshly opened form, never searched/filtered
        # on, so there's no reason to keep it in sync in the database.
        Document = self.env['meli.invoice.document'].sudo()
        for order in self:
            order.meli_has_unapplied_document = bool(Document.search_count([
                ('sale_order_id', '=', order.id),
                ('is_applied', '=', False),
            ]))
    meli_pack_has_unmapped_sibling_cancellation = fields.Boolean(
        string='Pack Has An Unmapped Sibling Cancellation', copy=False,
        help="Fix round 1 (2026-09-09, reviewer finding — Fix C): set "
             "the first time a cancellation notification arrives for a "
             "sibling within this active pack that has ZERO resolvable "
             "lines of its own (every one of its SKUs still unmapped — "
             "see _meli_notify_zero_line_sibling_cancellation and "
             "_meli_add_pack_sibling_lines's own docstring). Such a "
             "sibling never contributes a meli_order_id to "
             "order_line, so it's otherwise completely invisible to "
             "_meli_close_pack_if_every_sibling_cancelled's own "
             "'every sibling is cancelled' check — this field is what "
             "stops that check from auto-cancelling the whole sale "
             "while an unmapped sibling's own real Mercado Libre state "
             "was never actually confirmed. Fix 2026-09-09 "
             "(user-directed follow-up): cleared back to False "
             "automatically once that very sibling's SKU finally gets "
             "mapped and its own line is genuinely added via "
             "_meli_add_pack_sibling_lines (see that method) — the "
             "pack's own normal recovery path. A manual edit via "
             "Settings > Technical > Database Structure > Fields, or "
             "the ORM/shell, is still available as a fallback for any "
             "other case, but is no longer the only way to clear it.",
    )
    meli_auto_cancellation_processed = fields.Boolean(
        string='Automated Cancellation Processed', default=False, copy=False,
        help="Set once _meli_process_full_cancellation successfully runs "
             "the automated Full-cancellation sequence (stock return, "
             "sale cancellation) for this order — whether triggered by a "
             "normal mid-life 'cancelled' notification, or by this "
             "connector recovering an order that was already cancelled "
             "the first time it was ever seen. Never set for a non-Full "
             "order, or when the automation fails and falls back to the "
             "manual-review message.",
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

        order = self.sudo()._meli_create_from_order_data(config, order_data)
        if order:
            # This order didn't exist a moment ago (the `existing` search
            # above found nothing) — any meli.invoice.document that
            # already arrived for this order_id/pack_id before now would
            # have computed sale_order_id=False at ITS OWN creation time
            # and never revisit it on its own (see
            # meli.invoice.document._meli_recompute_and_reconcile's own
            # docstring for why). Force it now, immediately, rather than
            # waiting for the 10-minute safety-net cron.
            pack_id = str(order_data.get('pack_id') or '') or False
            self.env['meli.invoice.document']._meli_relink_orphaned_documents(
                order_id, pack_id,
            )
        return order

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
        except RetryableJobError:
            # Fix 5 (2026-09-09, final review — Important): re-raised
            # unchanged, BEFORE the generic `except Exception` below —
            # RetryableJobError is itself an Exception subclass, so
            # without this it would get caught here and the batch line
            # marked permanently 'error' on exactly the bulk-import path
            # most likely to trigger Mercado Libre rate limiting (many
            # orders queued at once). Letting it propagate lets
            # queue_job's own retry machinery (retry_pattern, backoff)
            # handle it instead, same as every other caller of
            # _meli_import_order.
            raise
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

    def action_meli_retry_invoicing_reconciliation(self):
        """Manual button: re-runs _meli_reconcile_invoicing right now
        (idempotent — see that method's own docstring), then, for a
        Full pack order only, also finishes the job for any credit
        note document that's related to this sale (sale_order_id set)
        but never actually got applied (meli.invoice.document.
        is_applied still False) — same core pipeline
        (_meli_apply_partial_cancellation: credit note relation, stock
        return, and line-quantity adjustment) the live 'order
        cancelled' webhook path already uses for a fresh cancellation,
        scoped here to documents a human has explicitly asked to
        retry, never automatically (see _meli_reconcile_invoicing's own
        pack-credit-note step for why it deliberately does NOT do this
        itself on every automatic call).

        Fix 2026-09-11: _meli_reconcile_invoicing's credit-note step,
        for a pack order, matches a credit note document to ITS OWN
        sibling's order_line by meli_order_id — if that check ever runs
        before the matching line exists yet (e.g. a credit-note
        document processed while an order/line creation was still
        delayed or stuck retrying — see the queue_job_cron_jobrunner
        retry_pattern incident, 2026-09-10), it posts a "could not be
        matched — review manually" chatter message and never retries
        on its own, even once the line legitimately exists — and even
        once it does relate the credit note, it never returns the
        sibling's own inventory (see _meli_reconcile_invoicing's own
        comment on this). This button is the manual escape hatch for
        both: a human confirms the underlying data is fine, then
        clicks this to finish the whole job for it.
        """
        self.ensure_one()
        self._meli_reconcile_invoicing()
        if not self.meli_pack_id:
            return
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)
        is_full = bool(
            config and config.warehouse_fulfillment_id
            and self.warehouse_id == config.warehouse_fulfillment_id
        )
        if not is_full:
            return
        from .meli_invoice_document import MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
        stuck_documents = self.env['meli.invoice.document'].sudo().search([
            ('sale_order_id', '=', self.id),
            ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
            ('is_applied', '=', False),
        ])
        stuck_sibling_ids = set(stuck_documents.mapped('meli_order_id')) - {False}
        for sibling_id in stuck_sibling_ids:
            self._meli_apply_partial_cancellation(sibling_id)
        if stuck_sibling_ids:
            # Fix 2026-09-11 (user-directed follow-up): a MANUAL click
            # here is a deliberate, one-off human action — unlike
            # _meli_reconcile_invoicing's own automatic callers (a
            # stock picking's _action_done(), the invoices webhook,
            # etc.), which must never eagerly close the whole sale just
            # because ONE sibling's document happened to be the one
            # that triggered them (see that method's own comment on
            # this exact regression). Safe to check here: for an order
            # that's really just a single individual order (the common
            # Ventiapp-adoption shape — meli_pack_id set but only ever
            # one real sibling), finishing that one sibling's own
            # cancellation IS finishing the whole thing.
            self._meli_close_pack_after_partial_cancellation()

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
        # Fix 1 (2026-09-09, final review — Critical): `or self.meli_adopted`
        # — an order this connector merely ADOPTED (linked to, never
        # created) must never be auto-confirmed by this path, even though
        # meli_sync_source is set on it too (adoption sets that same field
        # so future invoicing/cancellation notifications route correctly —
        # see _meli_create_from_order_data). Without this guard, the
        # polling cron's re-enqueue of any draft order with
        # meli_sync_source set (meli_config.py's _poll_recent_orders) could
        # reach here for a genuinely adopted Ventiapp order sitting in
        # draft and auto-confirm + auto-validate its delivery — exactly
        # the "adoption must only touch this connector's own control
        # fields" violation this field exists to prevent.
        if self.state != 'draft' or not self.meli_sync_source or self.meli_adopted:
            return
        existing_product_ids = set(self.order_line.product_id.ids)
        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data, self.meli_order_id)
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
            try:
                with self.env.cr.savepoint():
                    self.action_confirm()
            except Exception as err:
                _logger.exception(
                    "Mercado Libre order %s: action_confirm() failed "
                    "while retrying unmapped lines — left in draft for "
                    "manual review.", self.client_order_ref,
                )
                self._meli_post_with_mention(_(
                    "All Mercado Libre SKUs are now mapped, but this "
                    "sale could not be confirmed automatically: %s. "
                    "Please review and confirm it manually."
                ) % str(err))
                return
            self.message_post(body=_(
                "All Mercado Libre SKUs are now mapped — order confirmed."
            ))
            self._meli_ensure_delivery('MLF' in (self.origin or ''))

    def _meli_ensure_delivery(self, is_fulfillment):
        """Guarantees a normal Odoo delivery transfer exists for this
        confirmed sale, then (Full only) validates it — regardless of
        whether this method's own action_confirm() call is what put the
        order in 'sale' state, or some other automation on this database
        already had (seen in practice 2026-08-28: another automation can
        move a Mercado Libre order straight to 'sale' before this
        connector's own confirmation runs, which used to skip picking
        creation entirely — 48 such historical orders exist with zero
        pickings). _action_launch_stock_rule() is the exact same real
        Odoo mechanism action_confirm() itself uses internally
        (sale_stock/models/sale_order_line.py) to create deliveries —
        deliberately not hand-building a stock.picking here. Isolated in
        its own savepoint: a failure degrades to manual review, never
        loses or half-processes the sale.
        """
        self.ensure_one()
        if not self.picking_ids.filtered(lambda p: p.state != 'cancel'):
            try:
                with self.env.cr.savepoint():
                    self.order_line._action_launch_stock_rule()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: could not generate the "
                    "delivery transfer — needs manual review.",
                    self.client_order_ref,
                )
                self._meli_post_with_mention(_(
                    "This sale is confirmed but its delivery transfer "
                    "could not be generated automatically. Please "
                    "review and create it manually."
                ))
                return
        if is_fulfillment:
            self._meli_auto_validate_full_pickings()

    def _meli_auto_validate_full_pickings(self):
        """Full orders are fulfilled from Mercado Libre's own warehouse —
        the transfer here is bookkeeping, not something a person at XE
        needs to physically pack, so it's safe to validate automatically.
        Non-Full orders still need a person to actually pick/pack/ship,
        so those transfers are left for manual handling, for now (explicit
        user decision, 2026-08-28).

        Isolated in its own savepoint: if the transfer can't be validated
        for some reason (e.g. a genuine, non-stock-related Odoo
        validation error), that failure must not roll back the sale
        order itself — it just stays pending for manual review.
        """
        self.ensure_one()
        # Fix 7 (2026-09-09, final review — Minor): scoped to outgoing
        # (delivery) pickings only, matching the exact same
        # picking_type_id.code convention _meli_return_full_pickings
        # already uses — without this, a non-'done'/'cancel' incoming
        # return picking, or an intermediate step of a multi-step route,
        # could get force-completed here too, which is only ever correct
        # for the actual customer-facing delivery of a Full order.
        pickings = self.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
            and p.picking_type_id.code == 'outgoing'
        )
        for picking in pickings:
            try:
                with self.env.cr.savepoint():
                    # The Full fulfillment warehouse has no real stock
                    # tracked in Odoo (Mercado Libre's own warehouse
                    # manages it) — button_validate()'s normal
                    # reservation-gated flow can never succeed here.
                    # Odoo's OWN button_validate() already force-fills a
                    # move's done quantity from its demanded quantity,
                    # but ONLY for a picking still in 'draft' state
                    # (stock/models/stock_picking.py button_validate(),
                    # ~lines 1136-1141) — this picking is already
                    # 'confirmed' by the time we get here, so that
                    # auto-fill never applies. Do the same fill-in
                    # ourselves before calling button_validate(), which
                    # then finds real quantities, auto-picks the moves
                    # (_pre_action_done_hook), and validates cleanly with
                    # no backorder decision needed.
                    for move in picking.move_ids.filtered(
                        lambda m: m.state not in ('done', 'cancel')
                    ):
                        if float_is_zero(
                            move.quantity,
                            precision_rounding=move.product_uom.rounding,
                        ):
                            move.quantity = move.product_uom_qty
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

    def _meli_process_partial_cancellation(self, cancelled_order_id):
        """Cancellation/return of ONE individual Mercado Libre order
        within an active pack's consolidated sale.order, PLUS closing
        the whole sale once every sibling has reached this same state —
        see _meli_apply_partial_cancellation (the core work: credit
        note + stock return + quantity, called by BOTH this method and
        _meli_reconcile_invoicing) and _meli_close_pack_if_every_
        sibling_cancelled (the closure check, called ONLY from here)
        for what each half actually does and why they're split.

        Fix 2026-09-11: the closure check must stay tied to the real
        order-STATUS-changed signal this method is only ever called
        from (_meli_flag_status_change, itself only reachable from a
        live Mercado Libre notification) — never from a credit note
        DOCUMENT merely arriving (_meli_reconcile_invoicing, reachable
        from the invoices webhook/missed_feeds/batch import/manual
        retry, none of which mean Mercado Libre reported this order's
        STATUS as anything). Confirmed by a real regression: delegating
        _meli_reconcile_invoicing's own pack-credit-note step straight
        to this whole method auto-cancelled a still-legitimately-'sale'
        single-sibling pack the moment its credit note document was
        merely reconciled — with no order-cancelled notification ever
        involved.
        """
        self.ensure_one()
        self._meli_apply_partial_cancellation(cancelled_order_id)
        self._meli_close_pack_after_partial_cancellation()

    def _meli_sibling_lines(self, cancelled_order_id):
        """Resolves which of self.order_line belong to ONE individual
        Mercado Libre order (cancelled_order_id) within this pack's
        consolidated sale — shared by _meli_apply_partial_cancellation,
        _meli_sibling_is_fully_cancelled, and _meli_reconcile_invoicing's
        own pack-credit-note step, all three of which used to each
        inline this same lookup.

        Fix 2026-09-11: a plain filter by sale.order.line.meli_order_id
        alone never matches for an order ADOPTED from Ventiapp (see
        sale.order.meli_adopted's own help text) — adoption only ever
        sets THIS order's own control fields (meli_order_id,
        meli_pack_id), never touches order_line at all, so every one of
        its lines' own meli_order_id stays blank forever. Confirmed in
        production: a real credit note for such an order's OWN id
        (matching self.meli_order_id, not some OTHER sibling's) kept
        failing "could not be matched to any line" no matter how many
        times it was retried — there was genuinely no line for it to
        ever match. Falls back to EVERY one of self.order_line in that
        exact case (cancelled_order_id is THIS order's own id, and no
        line carries any meli_order_id of its own at all) — safe
        because there is, by definition, only one individual order
        involved when that's true; never applied to a genuine OTHER
        sibling within a real multi-order pack, where guessing which
        lines belong to it would risk touching another sibling's own,
        still-legitimate line.
        """
        self.ensure_one()
        lines = self.order_line.filtered(
            lambda l: l.meli_order_id == cancelled_order_id
        )
        if (
            not lines
            and cancelled_order_id == self.meli_order_id
            and not any(self.order_line.mapped('meli_order_id'))
        ):
            lines = self.order_line
        return lines

    def _meli_apply_partial_cancellation(self, cancelled_order_id):
        """The core work of a partial cancellation, without the pack-
        closure check — see _meli_process_partial_cancellation's own
        docstring for why that check is kept separate and only runs
        from there. Only THIS sibling's own line(s) — found via
        sale.order.line.meli_order_id — are touched: its own delivered
        stock is returned, its own portion of the invoice is credited
        (see _meli_relate_partial_cancellation_credit_note: a
        line-scoped, hand-built credit note — never the shared,
        whole-invoice account.move.reversal wizard
        _meli_reconcile_invoicing uses for a TOTAL cancellation, which
        mirrors EVERY line of the source invoice and would over-refund
        any OTHER sibling sharing that same consolidated invoice), and
        its own quantity is reduced. The sale itself is never
        cancelled, and no other sibling's line/inventory/revenue is
        touched.

        Deliberately runs the credit-note step BEFORE the stock return:
        stock.picking._action_done() (xe_meli_connector's own override)
        re-runs the shared, whole-order _meli_reconcile_invoicing() for
        EVERY completed picking tied to a Mercado Libre order —
        including the new return picking created below. Confirmed by
        reading sale_stock's own stock.picking.sale_id (computed from
        picking.group_id.sale_id) together with
        stock.return.picking._create_returns() (the return move is a
        plain copy() of the original delivery move, which — since
        stock.move.group_id isn't copy=False — keeps the exact same
        procurement group, and therefore the exact same sale_id) that
        this DOES resolve back to this same consolidated order — see
        task-6-report.md for the full chain. If this sibling's own
        credit note document hadn't already been related by the time
        that return picking completes, the shared reconciler would find
        the very same meli.invoice.document still unrelated and apply
        ITS OWN whole-invoice reversal to it — exactly the over-refund
        this method exists to prevent. Relating the credit note FIRST
        closes that window: by the time the return picking (if any)
        triggers the shared reconciler, it finds this document already
        related (via meli_invoice_document_id) and no-ops, same as any
        other idempotent re-run.
        """
        self.ensure_one()
        lines = self._meli_sibling_lines(cancelled_order_id)
        if not lines:
            # Fix 2 (2026-09-09, user-directed follow-up): before this
            # fix, a cancelled sibling with zero resolvable lines (every
            # one of its SKUs unmapped — see
            # _meli_add_pack_sibling_lines's own docstring, the exact
            # same real scenario Fix Round 1's own Important #2 already
            # had to account for elsewhere) made this method return here
            # completely silently: no chatter, no log, nothing for a
            # human to ever see. There is genuinely nothing to
            # return/credit-note (there's no line to touch), but the
            # underlying SKU-mapping gap is real and worth a human's
            # attention — posted AND routed straight to whoever holds
            # the "Queue Job Manager" admin permission, the same
            # audience/mechanism queue_job itself already uses for its
            # own failed-job notifications (see
            # queue.job._subscribe_users_domain/_message_post_on_failure).
            self._meli_notify_zero_line_sibling_cancellation(cancelled_order_id)
            return

        credit_note = self._meli_relate_partial_cancellation_credit_note(
            lines, cancelled_order_id,
        )

        # ---- Inventory: return ONLY this sibling's own delivered moves.
        # Same by-code stock.return.picking pattern as
        # _meli_return_full_pickings, scoped down to the moves whose
        # sale_line_id belongs to this sibling (stock_stock.move.
        # sale_line_id is set by core sale_stock's own stock rules).
        done_pickings = self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_id.code == 'outgoing'
        )
        sibling_moves = done_pickings.move_ids.filtered(
            lambda m: m.sale_line_id in lines and m.state == 'done'
        )
        returnable_moves = sibling_moves.filtered(
            lambda m: not m.returned_move_ids.filtered(lambda r: r.state != 'cancel')
        )
        new_pickings = self.env['stock.picking']
        for picking in returnable_moves.picking_id:
            picking_moves = returnable_moves.filtered(lambda m: m.picking_id == picking)
            return_lines = [
                (0, 0, {
                    'product_id': move.product_id.id,
                    'quantity': move.quantity,
                    'move_id': move.id,
                    'uom_id': move.product_id.uom_id.id,
                })
                for move in picking_moves
            ]
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
                'product_return_moves': return_lines,
            })
            new_picking_id, _picking_type_id = return_wizard._create_returns()
            new_picking = self.env['stock.picking'].browse(new_picking_id)
            result = new_picking.button_validate()
            if isinstance(result, dict):
                raise UserError(_(
                    "Automatic validation of the return transfer %s needs "
                    "manual confirmation (e.g. insufficient stock) — "
                    "cannot auto-process this sibling's cancellation."
                ) % new_picking.name)
            new_pickings |= new_picking

        # ---- Quantity: only THIS sibling's own line(s), set to reflect
        # what actually stayed delivered — read straight off the
        # sibling's own moves (delivered minus everything ever returned,
        # the return(s) just created above included), never by
        # subtracting off product_uom_qty. Fix Round 1, Important #3:
        # the old "ordered qty minus returned qty this call" math left a
        # line at its FULL original ordered quantity — with a
        # still-live, never-validated outbound move — whenever the
        # sibling's own outbound picking never actually validated (e.g.
        # Full auto-validation failed earlier for insufficient stock):
        # there is nothing to return in that case, so returned_qty was
        # always 0 and nothing ever adjusted, even though this
        # individual order is cancelled and nothing will actually ship
        # for it. sibling_moves is already scoped to this sibling's own
        # DONE outbound moves (see above), so it's naturally empty here
        # too when nothing was ever delivered — delivered_qty then comes
        # out 0, correctly zeroing the line instead of leaving it stale.
        # Fix Round 2, Important #1: totals accumulated across every one
        # of this sibling's own lines/moves — used below to tell apart
        # "never delivered at all" from "already delivered AND already
        # returned in an earlier, idempotent run" (new_pickings alone
        # can't tell those two apart: it's empty in BOTH cases, since
        # round 1's Finding 2 fix means a duplicate/re-delivered
        # cancellation notification for an already-fully-processed
        # sibling is now genuinely reachable, not just theoretical).
        total_delivered_qty = 0.0
        total_returned_qty = 0.0
        for line in lines:
            line_moves = sibling_moves.filtered(lambda m: m.sale_line_id == line)
            delivered_qty = sum(line_moves.mapped('quantity'))
            returned_qty = sum(
                line_moves.mapped('returned_move_ids').filtered(
                    lambda m: m.state == 'done'
                ).mapped('quantity')
            )
            total_delivered_qty += delivered_qty
            total_returned_qty += returned_qty
            line.product_uom_qty = max(delivered_qty - returned_qty, 0)

        # ---- Chatter: sale.order.line has no chatter tracking of its
        # own (Global Constraints), so the whole outcome — product,
        # quantity actually returned (if any), credit note, and the
        # FINAL line quantity — is reported here, on the order. Fix
        # Round 2, Important #1: the three distinct cases below are told
        # apart by ACTUAL delivered/returned state (total_delivered_qty/
        # total_returned_qty), never by whether THIS call created a new
        # picking (new_pickings) — that used to conflate "never delivered"
        # with "already delivered and already returned earlier", wrongly
        # claiming "transfer never completed" on an idempotent replay of
        # an already-fully-processed sibling.
        product_lines = ', '.join(
            _("%(product)s (now %(qty)s)") % {
                'product': line.product_id.display_name, 'qty': line.product_uom_qty,
            }
            for line in lines
        )
        if not total_delivered_qty:
            # (a) Never delivered at all — nothing physical to return.
            message = _(
                "Mercado Libre order %(order_id)s — one individual order "
                "within this active pack — was cancelled. It had no "
                "delivered stock to return (its own outbound transfer "
                "never completed), so only its own line quantity was "
                "adjusted to match; the sale itself and this pack's "
                "other sibling(s) were left completely untouched."
                "<br/>Line(s) after the update: %(product_lines)s."
            ) % {'order_id': cancelled_order_id, 'product_lines': product_lines or '-'}
        elif new_pickings:
            # (c) Delivered, and returned just now, in this very call.
            message = _(
                "Mercado Libre order %(order_id)s — one individual order "
                "within this active pack — was cancelled/returned. Only "
                "its own line(s) were touched; the sale itself and this "
                "pack's other sibling(s) were left completely untouched."
                "<br/>Line(s) after the return: %(product_lines)s."
            ) % {'order_id': cancelled_order_id, 'product_lines': product_lines or '-'}
            message += '<br/>' + _(
                "Stock returned via transfer(s): %s."
            ) % ', '.join(new_pickings.mapped('name'))
        else:
            # (b) Delivered, but already fully returned in an earlier run
            # — this call is a no-op replay (e.g. Mercado Libre re-sent
            # the same 'cancelled' notification). total_returned_qty
            # already matches total_delivered_qty at this point (nothing
            # left returnable), so there is genuinely nothing new to do
            # beyond confirming the line still reads correctly.
            message = _(
                "Mercado Libre order %(order_id)s — one individual order "
                "within this active pack — reported 'cancelled' again. "
                "Its stock was already returned in an earlier run (this "
                "looks like a repeated notification) — nothing new to "
                "return; the sale itself and this pack's other "
                "sibling(s) were left completely untouched."
                "<br/>Line(s): %(product_lines)s."
            ) % {'order_id': cancelled_order_id, 'product_lines': product_lines or '-'}
        if credit_note:
            message += '<br/>' + _(
                "Credit note %s covers this sibling's own portion of the "
                "invoice."
            ) % credit_note.name
        self.message_post(body=message)

    def _meli_close_pack_after_partial_cancellation(self):
        """Fix 1 (2026-09-09, user-directed follow-up): once every
        distinct sibling this pack has ever added a line for has
        reached its own final cancelled state, the sale itself was
        never actually closed — _meli_apply_partial_cancellation only
        ever touches the ONE sibling reported on. Scoped to the exact
        same Full-pack automation boundary _meli_flag_status_change
        already gates _meli_process_partial_cancellation behind (is_full
        and self.meli_pack_id) — recomputed here, not threaded through
        as a parameter, so this stays safe to call directly (as most of
        this method's own regression tests already do, several against
        a non-Full pack) without finalizing a sale that was never part
        of any automation to begin with.

        Fix 2026-09-11: split out of _meli_process_partial_cancellation
        into its own method — called ONLY from there, never from
        _meli_reconcile_invoicing's own call to
        _meli_apply_partial_cancellation. See
        _meli_process_partial_cancellation's own docstring for why.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)
        is_full = bool(
            config and config.warehouse_fulfillment_id
            and self.warehouse_id == config.warehouse_fulfillment_id
        )
        if is_full and self.meli_pack_id:
            # Fix round 1 (2026-09-09, reviewer finding — Fix A): isolated
            # in its own savepoint, same convention as
            # _meli_auto_validate_full_pickings and stock_picking.py's own
            # _action_done() override (order._meli_reconcile_invoicing()
            # call). This whole method already runs inside ONE savepoint
            # set up by the caller (_meli_flag_status_change) — and
            # action_unlock()/action_cancel() below can genuinely raise
            # (e.g. xe_pacific's action_unlock() override refuses a
            # picking still in 'transit' — see the comment on the
            # equivalent call in _meli_process_full_cancellation). Letting
            # that exception propagate unguarded would roll back the
            # ENTIRE outer savepoint — discarding THIS sibling's own
            # already-successful credit note, stock return, and quantity
            # update from earlier in this very same call, purely because
            # the LAST step (closing the sale) failed. A failure closing
            # the sale must degrade to manual review only, never undo
            # real, already-completed work for the sibling actually being
            # processed.
            try:
                with self.env.cr.savepoint():
                    self._meli_close_pack_if_every_sibling_cancelled()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: every visible sibling in "
                    "this pack now appears cancelled, but automatically "
                    "cancelling the sale itself failed — needs manual "
                    "review.", self.client_order_ref,
                )
                self.message_post(body=_(
                    "Every individual order within this pack now appears "
                    "cancelled, but the sale itself could not be "
                    "cancelled automatically — review manually. (This "
                    "sibling's own cancellation above still completed "
                    "successfully and was not affected.)"
                ))

    def _meli_close_pack_if_every_sibling_cancelled(self):
        """Fix 1: once every distinct sibling (meli_order_id) that ever
        contributed a line to this pack's consolidated order has reached
        its own final cancelled state (see _meli_sibling_is_fully_
        cancelled below for exactly what that means), the sale itself
        must be closed too — before this fix nothing ever did that, even
        once every single line individually read quantity 0.

        Deliberately does NOT reuse _meli_process_full_cancellation's
        stock-return or credit-note logic: every sibling's own return
        and credit note were already handled, one at a time, by
        _meli_process_partial_cancellation itself. This only needs the
        same final state transition _meli_process_full_cancellation ends
        with (action_unlock() when locked, then action_cancel()) — never
        its Phase 1 inventory/return work, which would be wrong to redo
        here.

        A sibling with ZERO lines of its own (every SKU unmapped — see
        _meli_add_pack_sibling_lines's own docstring, and
        _meli_notify_zero_line_sibling_cancellation/Fix 2 below) never
        contributes a meli_order_id to self.order_line at
        all, so it's simply invisible to the "every sibling is cancelled"
        check below — the same way it's already invisible to every other
        line-derived signal in this file (e.g. the routing guard in
        _meli_flag_status_change, which is why THAT one is keyed off
        self.meli_pack_id instead). Fix round 1 (2026-09-09, reviewer
        finding — Fix C): rather than silently auto-cancelling the whole
        sale once only the VISIBLE siblings are done — while an unmapped
        sibling's own real Mercado Libre status was never actually
        confirmed — meli_pack_has_unmapped_sibling_cancellation (set by
        _meli_notify_zero_line_sibling_cancellation the moment such a
        sibling is ever seen) makes this degrade to manual review
        instead, even once every visible sibling genuinely qualifies.
        """
        self.ensure_one()
        if self.state == 'cancel':
            return
        sibling_ids = set(self.order_line.mapped('meli_order_id')) - {False}
        if not sibling_ids:
            return
        if not all(
            self._meli_sibling_is_fully_cancelled(sibling_id)
            for sibling_id in sibling_ids
        ):
            return
        if self.meli_pack_has_unmapped_sibling_cancellation:
            self.message_post(body=_(
                "Every sibling with a resolvable line in this pack is "
                "now cancelled, but this pack has also seen a "
                "cancellation notification for a sibling with no "
                "line(s) of its own (an unmapped SKU) — its real status "
                "on Mercado Libre can't be confirmed, so the sale was "
                "NOT cancelled automatically. Review manually."
            ))
            return
        if self.locked:
            self.action_unlock()
        self.with_context(disable_cancel_warning=True).action_cancel()
        self.message_post(body=_(
            "Every individual order within this pack has now been "
            "cancelled — the sale itself was cancelled automatically."
        ))

    def _meli_sibling_is_fully_cancelled(self, sibling_id):
        """A sibling only counts as genuinely, finally cancelled when
        BOTH of these hold:
        - every one of its own line(s) reads quantity 0, AND
        - it already has its own live (non-cancelled) credit note
          related.

        Quantity alone isn't a safe signal by itself: a line can
        legitimately read 0 with nothing actually returned yet (see
        _meli_process_partial_cancellation's own "never delivered" case
        a few lines up — Fix Round 1, Important #3) while the credit
        note itself is still pending from Mercado Libre. Requiring both
        means the whole pack only gets auto-cancelled once the fiscal
        side is genuinely settled too, matching this module's existing
        carefulness around anything credit-note-adjacent (see, e.g., the
        double-refund guards in _meli_reconcile_invoicing and
        _meli_relate_partial_cancellation_credit_note).
        """
        self.ensure_one()
        lines = self._meli_sibling_lines(sibling_id)
        if not lines or any(line.product_uom_qty for line in lines):
            return False
        credited_lines = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
        ).invoice_line_ids.mapped('sale_line_ids')
        return not (lines - credited_lines)

    def _meli_notify_queue_job_managers(self, body):
        """Direct, one-off notification to whoever holds the "Queue Job
        Manager" administrative permission — same audience/mechanism
        queue_job itself already uses for its own failed-job
        notifications (see queue.job._subscribe_users_domain /
        queue.job._message_post_on_failure,
        queue/queue_job/models/queue_job.py). Never
        message_subscribe(): nobody should end up permanently
        following every single Mercado Libre order just because one
        automation attempt needed a human. Each manager still gets it
        through their own normal notification preference (email digest
        vs. the Discuss inbox).

        Degrades gracefully if the group itself can't be resolved
        (e.g. queue_job isn't installed in some environment) — the
        plain chatter message is still posted either way, just without
        the extra routing.

        Extracted 2026-09-10 from what was originally
        _meli_notify_zero_line_sibling_cancellation's own inline logic
        (see docs/superpowers/specs/2026-09-10-meli-cancellation-transaction-fix-design.md)
        — every "this automation was supposed to run by itself and
        broke" chatter message in this module now routes through here.
        """
        self.ensure_one()
        group = self.env.ref('queue_job.group_queue_job_manager', raise_if_not_found=False)
        manager_partner_ids = []
        if group:
            managers = self.env['res.users'].sudo().search([
                ('groups_id', '=', group.id),
                ('company_id', 'in', self.company_id.ids),
            ])
            manager_partner_ids = managers.mapped('partner_id').ids
        # with_context(mail_post_autofollow=False): sale.order's own
        # message_post() override (odoo/addons/sale/models/sale_order.py)
        # injects mail_post_autofollow=True by default whenever the
        # caller's context doesn't already say otherwise — which would
        # silently turn passing partner_ids here into a PERMANENT
        # message_subscribe() of every manager (confirmed in practice:
        # without this, each manager showed up in message_follower_ids
        # right after this call). Forcing it False here is what keeps
        # this the direct, one-off notification every caller needs.
        self.with_context(mail_post_autofollow=False).message_post(
            body=body, partner_ids=manager_partner_ids,
        )

    def _meli_notify_zero_line_sibling_cancellation(self, cancelled_order_id):
        """Fix 2: a cancelled sibling with zero resolvable lines (every
        one of its SKUs unmapped) has nothing for
        _meli_process_partial_cancellation to actually touch — but that
        SKU-mapping gap is real and worth a human's attention, so this
        posts a chatter message routed straight to whoever holds the
        "Queue Job Manager" administrative permission (see
        _meli_notify_queue_job_managers).
        """
        self.ensure_one()
        # Fix round 1 (2026-09-09, reviewer finding — Fix C): remembered
        # durably so _meli_close_pack_if_every_sibling_cancelled can
        # refuse to auto-cancel the whole sale later on — see that
        # field's own help text for why: this sibling never contributes
        # a meli_order_id to order_line, so without this flag it would
        # be entirely invisible to that method's own "every sibling is
        # cancelled" check.
        self.meli_pack_has_unmapped_sibling_cancellation = True
        self._meli_notify_queue_job_managers(_(
            "Mercado Libre order %(order_id)s — one individual order "
            "within this active pack — was cancelled, but it never "
            "had any line(s) of its own on this consolidated order "
            "(every one of its SKUs is still unmapped) — no "
            "inventory or invoice action was taken, there was "
            "nothing to touch. Review the SKU mapping for this "
            "order manually."
        ) % {'order_id': cancelled_order_id})

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

        Also reached, per-sibling, from _meli_create_from_order_data
        when a consolidated pack sale.order gets a later notification
        for one of its siblings reporting a non-'paid' status — but
        this method itself still only ever acts on the WHOLE order
        (cancel/return/credit-note everything, or one manual-review
        message for everything); it has no idea which specific sibling
        the notification was about. Per-line granularity (spec section
        5) is explicitly paused, not implemented here.
        """
        self.ensure_one()
        new_status = order_data.get('status')
        if not new_status:
            return
        # meli_last_status is ONE field on the shared, consolidated pack
        # sale.order — before this task, that was safe: the first
        # 'cancelled' notification for ANY sibling always cancelled the
        # whole order, so a second, later 'cancelled' notification for a
        # DIFFERENT sibling genuinely had nothing left to do. This task
        # makes 'cancelled' non-terminal (partial cancellation): sibling
        # B cancelling sets this field to 'cancelled', and sibling A
        # cancelling afterward would otherwise short-circuit here on
        # that same string — silently dropping a real cancellation for A
        # (no stock return, no credit note, not even a chatter note).
        # Fix Round 1, Important #1: this dedupe never applies to a pack
        # order at all — self.meli_pack_id is only ever set for orders
        # that ARE part of a pack (never for the plain, non-pack orders
        # every OTHER test in this module exercises), so this can't
        # regress the ordinary same-status dedupe there. Re-running the
        # partial-cancellation path for a genuinely repeated notification
        # of the SAME sibling is harmless — it's fully idempotent (see
        # test_partial_cancellation_is_idempotent) — so bypassing the
        # dedupe here at worst costs one redundant, idempotent re-run,
        # never a wrongly-dropped cancellation.
        if not self.meli_pack_id and new_status == self.meli_last_status:
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
        #
        # Fix A (2026-09-09, re-review of the final-review fix round —
        # Critical): `and not self.meli_adopted`. Adoption
        # (_meli_create_from_order_data) also sets meli_sync_source on a
        # pre-existing Ventiapp order, so without this the destructive
        # automation below (stock return, sale cancellation, invoice/
        # credit-note reconciliation) ran on an ADOPTED order too —
        # exactly the "commercial details left untouched" promise the
        # adoption's own chatter message makes, broken with zero human
        # review. An adopted order reporting 'cancelled' must fall
        # through to the same manual-review message every other
        # non-automated order gets, below.
        if new_status == 'cancelled' and self.meli_sync_source and not self.meli_adopted:
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

            # Partial cancellation: ONE individual Mercado Libre order
            # within an ACTIVE pack's consolidated sale.order — confirmed
            # in practice (2026-09-08, 10 real packs) where one sibling
            # stayed 'paid' while another went 'cancelled'. order_data is
            # always the SPECIFIC sibling's own order resource here, not
            # necessarily this sale.order's own meli_order_id: a
            # notification for any sibling other than the first one is
            # routed to this exact method by
            # _meli_create_from_order_data's own pack branch (see its
            # docstring), passing that sibling's own order_data through
            # untouched — so order_data['id'] reliably names the one
            # sibling actually being reported on.
            #
            # Fix Round 1, Important #2: keyed off self.meli_pack_id —
            # NOT off whether notified_order_id shows up among this
            # order's own order_line.meli_order_id values (the original
            # shape). _meli_add_pack_sibling_lines can leave a sibling
            # with ZERO lines at all (every one of its SKUs unmapped —
            # see that method's own docstring), and in that state the
            # sibling's id never appears in sibling_ids, which used to
            # make this guard fail and fall through into the whole-order
            # `is_full` cancellation right below — cancelling the ENTIRE
            # sale and returning the OTHER, still-paid sibling's stock,
            # exactly backwards from what was actually reported.
            # meli_pack_id is set once, at creation, for every order that
            # is genuinely part of a pack — regardless of how many
            # siblings currently have resolvable lines — and is never set
            # for a plain, non-pack order (every OTHER cancellation test
            # in this module), so this can't affect those at all.
            # _meli_process_partial_cancellation itself is a safe no-op
            # when the notified sibling has no lines to touch (see its
            # own early return), so routing here even for a still-
            # unmapped sibling costs nothing.
            notified_order_id = str(order_data.get('id') or '')
            if is_full and self.meli_pack_id:
                # Always returns below, success or failure — deliberately
                # NEVER falls through into the whole-order `is_full`
                # cancellation right after this block: that automation
                # cancels the ENTIRE sale and returns EVERY line's stock
                # (see _meli_process_full_cancellation), which would
                # wrongly wipe out the other sibling(s) in this pack that
                # are still 'paid'. A failed partial cancellation must
                # degrade to manual review only — never escalate into a
                # more destructive action than the one that was actually
                # requested.
                try:
                    with self.env.cr.savepoint():
                        self._meli_process_partial_cancellation(notified_order_id)
                except Exception:
                    _logger.exception(
                        "Mercado Libre order %s: automatic partial "
                        "cancellation failed for individual order %s "
                        "within this pack — needs manual review.",
                        self.client_order_ref, notified_order_id,
                    )
                    self._meli_notify_queue_job_managers(_(
                        "Mercado Libre reports that individual order "
                        "%(order_id)s (part of this active pack) was "
                        "cancelled, but automatic processing failed. "
                        "Review manually — the sale itself and every "
                        "sibling's own line were left untouched."
                    ) % {'order_id': notified_order_id})
                return

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
                    self._meli_notify_queue_job_managers(_(
                        "Mercado Libre reports this order was "
                        "<b>cancelled</b>, but automatic processing "
                        "failed — review manually whether the sale "
                        "needs to be cancelled, stock returned, and/or "
                        "a credit note issued."
                    ))
                    return
                else:
                    # Deliberately NO commit here between Phase 1 and
                    # Phase 2 (removed 2026-09-10 — see
                    # docs/superpowers/specs/2026-09-10-meli-cancellation-transaction-fix-design.md
                    # for the full investigation). A manual cr.commit()
                    # used to live here, defending against the fear that
                    # l10n_mx_edi might commit internally on its own
                    # during Phase 2 — but every l10n_mx_edi cr.commit()
                    # call site (enterprise/l10n_mx_edi/models/
                    # l10n_mx_edi_document.py) lives inside _send_api/
                    # _cancel_api/_update_sat_state, the real PAC-
                    # stamping/cancellation/status-polling methods this
                    # module never reaches — Mercado Libre is the sole
                    # source of the CFDI. The commit's real effect was
                    # worse than the risk it defended against: this whole
                    # method runs inside a queue.job, and in this
                    # deployment (queue_job is not a server_wide_module)
                    # jobs are processed by queue_job_cron_jobrunner,
                    # whose _process() wraps job.perform() in its own
                    # cr.savepoint() — committing while nested inside
                    # that ends the whole transaction, including that
                    # savepoint, out from under it, so its own RELEASE
                    # SAVEPOINT fails (InvalidSavepointSpecification),
                    # which then makes queue.job's own failure-logging
                    # write fail too (InFailedSqlTransaction) — leaving
                    # the job stuck forever instead of properly marked
                    # failed. Reproduced directly against a real database
                    # with plain psycopg2 SAVEPOINT/COMMIT/RELEASE
                    # commands (no business data touched), and matched
                    # real evidence of stuck/aborted jobs. Phase 1 and
                    # Phase 2 now run inside ONE atomic unit instead: if
                    # anything fails anywhere, the whole job rolls back
                    # cleanly (no more partial-success ambiguity), and
                    # the chatter message below persists durably once the
                    # job actually completes, since nothing interrupts
                    # the transaction partway through anymore.
                    # _meli_reconcile_invoicing is the single source of
                    # truth for this order's fiscal documents now (Tasks
                    # 2-4): it looks at what meli.invoice.document rows
                    # actually exist and relates/creates whatever Odoo
                    # side is still missing — including, for a Full order
                    # like this one, relating a real Mercado Libre credit
                    # note if one was already issued. It posts its own
                    # chatter messages for whatever it does (invoice
                    # created/related, refacturación cancel, credit note
                    # related, or a manual-review note when a credit note
                    # exists but there's nothing yet to apply it to), so
                    # Phase 2's own message below only needs to cover
                    # Phase 1's stock/sale actions — duplicating the
                    # reconciler's own wording here would just double up
                    # the chatter.
                    reconcile_failed = False
                    try:
                        self._meli_reconcile_invoicing()
                    except Exception:
                        # Safety net only: a real PostgreSQL error (e.g. a
                        # NOT NULL violation somewhere inside the
                        # reconciler) aborts the transaction and would
                        # make the message_post() below fail with
                        # InFailedSqlTransaction too, leaving the operator
                        # with no chatter message at all and nothing but a
                        # failed queue job. So recover the transaction
                        # FIRST, before trying to report anything.
                        # _meli_recover_aborted_transaction()
                        # is itself a no-op unless the transaction is
                        # genuinely broken (or under tests), so it's safe
                        # to call unconditionally here even for an
                        # ordinary, non-DB exception.
                        self._meli_recover_aborted_transaction()
                        _logger.exception(
                            "Mercado Libre order %s: stock return and sale "
                            "cancellation succeeded, but reconciling "
                            "invoicing failed — needs manual review.",
                            self.client_order_ref,
                        )
                        reconcile_failed = True
                    message = _(
                        "Mercado Libre reports this order was "
                        "<b>cancelled</b>. Handled automatically:"
                        "<br/>%s"
                    ) % '<br/>'.join(actions)
                    if reconcile_failed:
                        message += '<br/>' + _(
                            "Invoice reconciliation did not fully succeed "
                            "and needs manual review — the stock return "
                            "and sale cancellation above were still "
                            "completed successfully."
                        )
                    self._meli_notify_queue_job_managers(message)
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
        return + sale cancellation only (invoice reconciliation is
        Phase 2, run separately by the caller — see
        _meli_flag_status_change). Full orders are fulfilled from
        Mercado Libre's own warehouse, so this can be resolved end-to-end
        without a human: the inventory never left XE's control in a way
        that needs physical handling. Returns the list of HTML action
        descriptions to report in the chatter. Raises on any failure —
        the caller wraps this in a savepoint and falls back to the
        manual-review message.

        Deliberately does NOT call _meli_reconcile_invoicing(): the
        l10n_mx_edi bookkeeping methods it reuses (e.g.
        _l10n_mx_edi_cfdi_invoice_document_cancel /
        _l10n_mx_edi_cfdi_invoice_document_sent) can force their own
        commit in production, which would silently invalidate this
        method's savepoint. Invoice reconciliation must run after this
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
        self.meli_auto_cancellation_processed = True
        return actions

    def _meli_recover_cancelled_on_arrival_full(self, config):
        """Runs immediately after a Full order that was 'cancelled' the
        very first time this connector ever saw it gets created,
        confirmed, and delivered by _meli_create_from_order_data (see
        docs/superpowers/specs/2026-09-09-meli-cancelled-order-recovery-design.md).
        Reuses the exact same two-phase pipeline _meli_flag_status_change
        already uses for a normal, mid-life Full cancellation —
        _meli_process_full_cancellation (stock return + real
        action_cancel()) followed by _meli_reconcile_invoicing() (creates
        +relates the invoice, then relates the credit note, in that
        order, already in one call) — no new invoicing logic here.

        Before reconciling, forces a recompute of any meli.invoice.document
        rows that already existed for this order/pack BEFORE this sale
        order did: meli.invoice.document.sale_order_id is a stored
        compute field with @api.depends('meli_order_id') only — it never
        recomputes just because a NEW sale.order shows up later matching
        that value, since nothing about creating a sale.order touches the
        document's own meli_order_id. Without this, _meli_reconcile_
        invoicing()'s own search (('sale_order_id', '=', self.id)) would
        find nothing at all, even though the document genuinely belongs
        to this order.
        """
        self.ensure_one()
        if self.meli_pack_id:
            # A pack order recovered here has, by definition, just been
            # seen for the very first time — there is no "other sibling
            # already known and still paid" state to protect the way
            # _meli_flag_status_change's own is_full-and-meli_pack_id
            # branch protects a MID-LIFE partial cancellation. Running
            # the whole-order destructive pipeline (_meli_process_full_
            # cancellation) anyway would risk exactly what a later,
            # genuinely-paid sibling needs: _meli_add_pack_sibling_lines
            # runs regardless of state and would silently add its line
            # to an already-cancelled order, losing that sibling's own
            # delivery/invoice/revenue with no trace beyond a throwaway
            # chatter note (final whole-branch review finding, Critical
            # C2, 2026-09-09). Degrading to manual review here — same
            # principle as every other can't-safely-automate gate in
            # this module.
            self._meli_notify_queue_job_managers(_(
                "Mercado Libre reported this pack order as already "
                "cancelled when it was first imported. It was created, "
                "confirmed, and delivered as a precaution, but automatic "
                "cancellation processing was skipped because this is "
                "part of a pack — review manually whether to cancel it "
                "and apply any invoice/credit note, and whether other "
                "siblings are still expected."
            ))
            return
        orphaned_documents = self.env['meli.invoice.document'].sudo().search([
            ('meli_order_id', 'in', (
                [self.meli_pack_id, self.meli_order_id] if self.meli_pack_id
                else [self.meli_order_id]
            )),
            ('sale_order_id', '=', False),
        ])
        if orphaned_documents:
            orphaned_documents._compute_sale_order_id()

        try:
            with self.env.cr.savepoint():
                actions = self._meli_process_full_cancellation(config)
        except Exception:
            _logger.exception(
                "Mercado Libre order %s: automatic cancellation-on-"
                "arrival processing failed — the order stays as created, "
                "confirmed, and delivered, but needs manual review.",
                self.client_order_ref,
            )
            self._meli_notify_queue_job_managers(_(
                "Mercado Libre reported this order as already cancelled "
                "when it was first imported. It was created, confirmed, "
                "and delivered as a precaution, but automatic "
                "cancellation processing failed — review manually "
                "whether to cancel it and apply any invoice/credit note."
            ))
            return

        # Deliberately NO commit here between Phase 1 and Phase 2 —
        # same reasoning as _meli_flag_status_change's own, identical
        # fix (2026-09-10, see
        # docs/superpowers/specs/2026-09-10-meli-cancellation-transaction-fix-design.md):
        # a manual cr.commit() here corrupted queue_job_cron_jobrunner's
        # own enclosing savepoint, since this method also runs inside a
        # queue.job. Both phases now run inside one atomic unit.

        reconcile_failed = False
        try:
            self._meli_reconcile_invoicing()
        except Exception:
            self._meli_recover_aborted_transaction()
            _logger.exception(
                "Mercado Libre order %s: stock return and sale "
                "cancellation succeeded, but reconciling invoicing "
                "failed — needs manual review.", self.client_order_ref,
            )
            reconcile_failed = True

        message = _(
            "Mercado Libre reported this order as already <b>cancelled</b> "
            "when it was first imported. It was created, confirmed, and "
            "delivered as a precaution, then automatically cancelled and "
            "reconciled:<br/>%s"
        ) % '<br/>'.join(actions)
        if reconcile_failed:
            message += '<br/>' + _(
                "Invoice reconciliation did not fully succeed and needs "
                "manual review — the stock return and sale cancellation "
                "above were still completed successfully."
            )
        self._meli_notify_queue_job_managers(message)

    def _meli_reconcile_invoicing(self):
        """Single reconciler for this sale's fiscal documents: looks at
        what actually exists in meli.invoice.document right now and does
        whatever is still missing on the Odoo side — never tries to infer
        Mercado Libre's own intent (it doesn't document the exact rules
        for when a re-invoicing event is a straight cancel+reissue vs. a
        credit note+reissue), just reacts to what's there. Safe to call
        repeatedly — every step is idempotent.

        Mercado Libre is the only source of the CFDI (see
        res.partner.cfdi_issued_by_third_party in xe_l10n_mx_edi) — this
        method never calls Odoo's own PAC-stamping methods. It creates a
        normal Odoo invoice (same accounts/journal as any other) and
        relates the real XML Mercado Libre already generated.
        """
        self.ensure_one()
        # Imported here, not at module level: meli_invoice_document.py
        # already imports MELI_FISCAL_TIMEZONE from this module (loaded
        # before sale_order.py in models/__init__.py) — a module-level
        # import back from here to meli_invoice_document.py would create
        # a genuine circular import (confirmed in practice: the module
        # failed to load with "cannot import name
        # 'MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES' from partially
        # initialized module"). By the time this method actually runs,
        # both modules are already fully loaded, so a local import here
        # is completely safe.
        from .meli_invoice_document import (
            MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES, MELI_INVOICE_DEAD_STATUSES,
        )

        if not self.picking_ids.filtered(lambda p: p.state == 'done'):
            return

        # 'id desc' as the real tie-breaker, not just 'create_date desc'
        # alone: create_date is a plain SQL column DEFAULT of now()
        # (transaction start time, not clock_timestamp()), so two
        # documents created within the same transaction — the ordinary
        # case for a TransactionCase test, and not impossible in
        # production either (e.g. two documents upserted back-to-back
        # inside the same request) — can carry the EXACT SAME
        # create_date. Confirmed in practice (2026-09-08): with
        # 'create_date desc' alone, a second, newer document tied on
        # create_date with the first was not reliably picked as the
        # newest, silently breaking refacturación detection. 'id' is
        # monotonically increasing with creation order and always
        # resolves the tie correctly.
        # ('xml_file', '!=', False) — meli_invoice_document.py itself
        # (_meli_import_invoice_document, ~line 379-393) deliberately
        # upserts a document with xml_file=False when Mercado Libre's
        # own XML fetch 404s ("storing the record without a file for
        # manual follow-up"), same as an existing caller in that file
        # already guards against
        # (_meli_import_invoice_document_for_batch_line: "if not
        # document or not document.xml_file"). Without this filter here
        # too, this method would find such a document, then crash on
        # base64.b64decode(False) inside _meli_relate_invoice_document —
        # but only AFTER already creating and posting a new invoice
        # (and, on refacturación, already cancelling the old one),
        # leaving a half-done invoice with no CFDI relation. A document
        # missing its XML isn't "ready" yet, so it's simplest and
        # safest to treat it exactly like "no document found yet" (the
        # early return right below) — nothing has been touched, and the
        # very next poll/webhook/upsert that brings the real XML will
        # pick it up normally, same as this method already does for a
        # sale with no document at all.
        invoice_document = self.env['meli.invoice.document'].sudo().search([
            ('sale_order_id', '=', self.id),
            ('transaction_type', 'not in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
            ('xml_file', '!=', False),
        ], limit=1, order='create_date desc, id desc')

        current_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )[:1]
        # Whether there's an invoice-creation/refacturación step to do
        # is now a plain condition guarding the block below, NOT a hard
        # `return` straight out of the whole method (that was the
        # original Task 2 shape). Reason: the credit-note step further
        # down (Task 4) is a genuinely separate reconciliation and must
        # still run even when there's no non-credit-note document at
        # all yet, or the current invoice already reflects the newest
        # one — the ordinary real-world case is exactly that: Mercado
        # Libre issues ONE sale document, then LATER, with no new sale
        # document alongside it, issues a devolution/credit-note
        # document against it. A hard early return here would make
        # every call after the first silently ignore that credit note.
        invoice_up_to_date = bool(
            current_invoice and invoice_document
            and current_invoice.meli_invoice_document_id == invoice_document
        )
        if invoice_document and not invoice_up_to_date:
            # Fix round 1 (2026-09-09, reviewer finding — Fix B): whether
            # the invoice-creation/refacturación step below actually runs
            # is its OWN condition (proceed_with_invoice_step), separate
            # from whether Step 3 (credit-note relating, further down)
            # runs — same "a plain condition, never a hard return out of
            # the whole method" principle already established for this
            # entire if-block (see the comment on invoice_up_to_date just
            # above). _meli_reconcile_invoicing is idempotent and
            # re-entered on every picking completion / webhook / document
            # upsert — if the refacturación-blocked case below `return`ed
            # out of the whole method, it would keep blocking Step 3
            # (which could easily be about a completely different,
            # unrelated sibling's own later cancellation) on every single
            # future call for this order, not just this one's own,
            # already-doomed refacturación attempt.
            proceed_with_invoice_step = True
            if current_invoice:
                # Fix 3 (2026-09-09, user-directed follow-up): if this
                # order already has a live (non-cancelled) out_refund
                # related to a real, already-processed partial-
                # cancellation credit note (transaction_type in
                # MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES), automatic
                # refacturación must not proceed — cancelling
                # current_invoice out from under a credit note that
                # already reverses it (reversed_entry_id) would corrupt
                # that relationship, and blindly recreating a fresh
                # invoice here has no safe way to also carry the
                # existing credit note's own adjustment forward. Same
                # "degrade to manual review, never guess" principle as
                # every other can't-safely-automate gate in this method
                # (see, e.g., the non-Full credit-note gate right below,
                # or the double-refund guards a few lines further down).
                live_partial_cancellation_credit_note = self.invoice_ids.filtered(
                    lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
                    and m.meli_invoice_document_id
                    and m.meli_invoice_document_id.transaction_type
                    in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
                )[:1]
                if live_partial_cancellation_credit_note:
                    self.message_post(body=_(
                        "Mercado Libre issued a new invoice document "
                        "(%(new)s) for this order, but a live credit "
                        "note (%(credit_note)s) from a prior partial "
                        "cancellation already exists — automatic "
                        "refacturación was skipped to avoid corrupting "
                        "that credit note's relation to the invoice it "
                        "reverses; review manually."
                    ) % {
                        'new': invoice_document.meli_invoice_id or invoice_document.id,
                        'credit_note': live_partial_cancellation_credit_note.name,
                    })
                    proceed_with_invoice_step = False
                else:
                    # Refacturación: a newer document exists than the one this
                    # invoice reflects. Whether Mercado Libre got here by
                    # cancelling+reissuing or by crediting+reissuing, the result
                    # on our side is the same: this invoice is stale, cancel it.
                    #
                    # button_cancel() — NOT action_cancel(), which the design
                    # spec and plan both name but which does not exist on
                    # account.move (confirmed: AttributeError) — is the real
                    # method. But by the time this branch runs, current_invoice
                    # has necessarily already been through
                    # _meli_relate_invoice_document (this same method creates
                    # and relates every invoice it makes), so it carries
                    # l10n_mx_edi_cfdi_state='sent' — which makes Odoo's own
                    # account.move._l10n_mx_edi_need_cancel_request() true and
                    # a bare button_cancel() raise UserError("... You need to
                    # request a cancellation instead."), confirmed in practice.
                    # That guard exists to stop a plain cancel from silently
                    # discarding what Odoo believes is a real, SAT-stamped CFDI
                    # — exactly what this move now has, on purpose.
                    #
                    # The fix is NOT to route through button_request_cancel()
                    # (that only opens a cancellation wizard for a human, and
                    # completing it for real would ask Odoo's own l10n_mx_edi
                    # session to request a SAT cancellation for a CFDI Odoo
                    # never sent — exactly what "Mercado Libre is the only
                    # source of the CFDI" forbids) and NOT a raw
                    # write({'state': 'cancel'}) bypassing the guard entirely
                    # (would leave Odoo's books showing this invoice cancelled
                    # while its CFDI stays valid at the SAT). Instead:
                    # _l10n_mx_edi_cfdi_invoice_document_cancel(cfdi,
                    # cancel_reason) — the same enterprise l10n_mx_edi method
                    # normally used to record a cancellation OUTCOME after a
                    # real PAC cancel call succeeds — is reused here purely as
                    # bookkeeping: it creates a new l10n_mx_edi.document with
                    # state='invoice_cancel', reusing the SAME attachment the
                    # invoice already has (cfdi.attachment_id.id — no new
                    # attachment, no PAC call, no network I/O at all). Once that
                    # document exists, l10n_mx_edi_cfdi_state no longer computes
                    # to 'sent' (see _compute_l10n_mx_edi_cfdi_state_and_attachment,
                    # which reads the NEWEST document's state), so
                    # _l10n_mx_edi_need_cancel_request() returns False and the
                    # ordinary button_cancel() below proceeds normally. This
                    # only touches Odoo's own local bookkeeping of what the
                    # already-related XML's fate was — it never contacts a PAC,
                    # matching this whole method's central rule.
                    live_cfdi_document = current_invoice.l10n_mx_edi_invoice_document_ids.filtered(
                        lambda d: d.state == 'invoice_sent'
                    )[:1]
                    if live_cfdi_document:
                        current_invoice._l10n_mx_edi_cfdi_invoice_document_cancel(
                            live_cfdi_document, MELI_REFACTURA_CANCEL_REASON,
                        )
                    current_invoice.button_cancel()
                    self.message_post(body=_(
                        "Mercado Libre issued a new invoice document "
                        "(%(new)s) replacing the one this order's invoice "
                        "%(old)s reflected — the old invoice was cancelled."
                    ) % {'new': invoice_document.meli_invoice_id or invoice_document.id,
                         'old': current_invoice.name})

            if proceed_with_invoice_step:
                new_invoice = self._create_invoices()
                new_invoice = new_invoice.filtered(lambda m: m.move_type == 'out_invoice')
                new_invoice.action_post()
                self._meli_relate_invoice_document(new_invoice, invoice_document)
                self.message_post(body=_(
                    "Invoice %(invoice)s created and related to Mercado Libre "
                    "document %(document)s."
                ) % {'invoice': new_invoice.name,
                     'document': invoice_document.meli_invoice_id or invoice_document.id})

        # Step 3: relate a credit note (nota de crédito) Mercado Libre
        # itself already issued — but ONLY for Full orders. A credit
        # note always means a real return/cancellation happened; for
        # non-Full orders a human must physically confirm the return
        # before Odoo's books or inventory are touched, same internal-
        # control principle already applied everywhere else in this
        # module for non-Full cancellations (see the manual-review
        # fallback in _meli_flag_status_change). Automating a credit
        # note for a non-Full order would silently book a fiscal
        # document ahead of that physical confirmation — exactly the
        # gap this Full-only gate exists to close.
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)
        is_full = bool(
            config and config.warehouse_fulfillment_id
            and self.warehouse_id == config.warehouse_fulfillment_id
        )
        # Final review fix (2026-09-08): three fixes bundled into this one
        # search, all confirmed against real Mercado Libre behaviour:
        #
        # Fix 2 — ('xml_file', '!=', False), matching the exact same
        # filter the invoice_document search above already carries (see
        # its own comment for the full rationale): a document upserted
        # with no XML yet isn't "ready" — without this filter here a
        # credit note could get created and posted, then crash on
        # base64.b64decode(False) inside _meli_relate_invoice_document.
        #
        # Fix 1 (second half) — ('status', 'not in', ...DEAD_STATUSES):
        # meli.invoice.document.status is populated from real webhook
        # data and real accounts have seen 'rejected'/'cancelled'
        # devolución statuses (see that field's own help text and this
        # model's own tree/search views, which already treat these two
        # values as dead). A document the SAT itself rejected or
        # cancelled must never be used to create a real Odoo credit
        # note.
        #
        # Fix 3 — no `limit=1` anymore, and ordered OLDEST first
        # ('create_date asc, id asc', the reverse of every other
        # tie-broken search in this method): Mercado Libre can issue
        # more than one credit-note document over time for the same
        # order/sibling (see meli.invoice.document._meli_upsert's own
        # docstring — the same "replacement" shape confirmed for
        # facturas applies to devoluciones too). Picking only the single
        # newest one permanently masked an OLDER, never-yet-related
        # document (e.g. one that arrived before any invoice existed and
        # got a "no posted invoice yet" manual-review message): once a
        # newer document appeared, the older one was never revisited
        # again. The loop below walks every actionable document oldest
        # first, so an older one that's still actionable always gets its
        # turn before a newer one does.
        credit_note_documents = self.env['meli.invoice.document'].sudo().search([
            ('sale_order_id', '=', self.id),
            ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
            ('xml_file', '!=', False),
            ('status', 'not in', list(MELI_INVOICE_DEAD_STATUSES)),
        ], order='create_date asc, id asc')
        if not credit_note_documents:
            return

        # Fix Round 1 (Critical): a multi-sibling pack order sharing ONE
        # consolidated invoice must NEVER fall through to the
        # whole-invoice account.move.reversal path below — that wizard
        # mirrors EVERY line of source_invoice into the new credit note,
        # which would over-refund every OTHER sibling's still-legitimate
        # line sharing that same invoice. This is reachable even when
        # _meli_process_partial_cancellation already ran for the
        # cancelled sibling and found no credit-note document yet to
        # relate — a completely ordinary ordering (the physical return
        # can easily complete before Mercado Libre's own devolución CFDI
        # arrives): that document's own later upsert
        # (meli.invoice.document._meli_upsert) unconditionally calls
        # THIS method, on the SHARED consolidated order, with no
        # pack-awareness of its own. Delegate to the exact same,
        # already-tested, line-scoped helper
        # _meli_process_partial_cancellation itself uses — scoped to the
        # specific sibling each document actually belongs to
        # (document.meli_order_id), never to the whole invoice. That
        # helper's own already_related check is keyed by document
        # identity, not by "one credit note per order", so a
        # second/later credit-note document for a sibling that's already
        # been credited naturally no-ops too, with no extra bookkeeping
        # needed here.
        #
        # Fix Round 2, Important #2 (narrower recurrence of the Round 1
        # Critical bug): gated on self.meli_pack_id — truthy for every
        # pack order, set once at creation (see _meli_create_from_order_data)
        # — NOT on counting distinct meli_order_id values actually present
        # among self.order_line, the same fragile-source mistake Round 1's
        # Finding 3 already had to fix for _meli_flag_status_change's own
        # routing guard (see is_full and self.meli_pack_id a few methods
        # up). A sibling with zero resolvable lines (every one of its SKUs
        # unmapped — a real, documented case, see
        # _meli_add_pack_sibling_lines's own docstring) never contributes
        # its own id to a line-derived set: a 2-order pack where the
        # CREDIT-NOTED sibling is exactly that unmapped one would then
        # read as a single-sibling order (len(sibling_ids) == 1, only the
        # OTHER, mapped sibling counts) and fall through to the
        # whole-invoice account.move.reversal below — over-refunding the
        # mapped sibling. meli_pack_id doesn't have this blind spot: it's
        # set directly on the order the moment it's created as part of a
        # pack, regardless of which siblings currently have resolvable
        # lines.
        if self.meli_pack_id:
            if not is_full:
                newest_document = credit_note_documents[-1]
                self.message_post(body=_(
                    "Mercado Libre generated a credit note (%(document)s) "
                    "for this order — review manually and apply it; "
                    "credit notes are not automated for non-Full orders."
                ) % {'document': newest_document.meli_invoice_id or newest_document.id})
                return
            # Fix 3 (pack side): walk every actionable document oldest
            # first, but delegate to _meli_relate_partial_cancellation_
            # credit_note only ONCE per distinct sibling (cancelled_order_id)
            # — that helper re-searches and iterates ALL of that specific
            # sibling's own credit-note documents by itself (its own Fix
            # 1/2/3 below), so calling it more than once per sibling here
            # would just repeat the exact same work.
            handled_sibling_ids = set()
            for document in credit_note_documents:
                cancelled_order_id = document.meli_order_id
                if cancelled_order_id in handled_sibling_ids:
                    continue
                handled_sibling_ids.add(cancelled_order_id)
                sibling_lines = self._meli_sibling_lines(cancelled_order_id)
                if not sibling_lines:
                    self.message_post(body=_(
                        "Mercado Libre generated a credit note (%(document)s), "
                        "but its own order id (%(order_id)s) could not be "
                        "matched to any line on this pack's consolidated "
                        "order — review manually."
                    ) % {
                        'document': document.meli_invoice_id or document.id,
                        'order_id': cancelled_order_id or '?',
                    })
                    continue
                # Deliberately only relates the credit note here, same as
                # always — NOT the fuller _meli_apply_partial_cancellation
                # (credit note + stock return + quantity), which would
                # make ANY automatic call to _meli_reconcile_invoicing
                # (this method also runs as a side effect of validating
                # an unrelated stock picking on this same order — see
                # _meli_process_partial_cancellation's own docstring)
                # eagerly sweep up and act on every OTHER sibling's own
                # pending devolución too, ahead of ITS OWN explicit
                # order-status-changed event. Confirmed by a real
                # regression (2026-09-11): doing that here auto-cancelled
                # a still-legitimately-'sale' 2-sibling pack the moment
                # only ONE sibling's own return picking validated, before
                # the OTHER sibling's own cancellation was ever reported.
                # The fuller pipeline for a document that's stuck exactly
                # like this one — related to its sale_order_id but never
                # actually applied — is available on demand instead, via
                # action_meli_retry_invoicing_reconciliation.
                self._meli_relate_partial_cancellation_credit_note(
                    sibling_lines, cancelled_order_id,
                )
            return

        if not is_full:
            newest_document = credit_note_documents[-1]
            self.message_post(body=_(
                "Mercado Libre generated a credit note (%(document)s) "
                "for this order — review manually and apply it; credit "
                "notes are not automated for non-Full orders."
            ) % {'document': newest_document.meli_invoice_id or newest_document.id})
            return

        source_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        )[:1]
        # Fix 1 (first half, whole-order side): whether this scope
        # (the whole invoice) ALREADY has a live, non-cancelled
        # out_refund — regardless of which specific meli.invoice.document
        # row it's related to. Computed ONCE, before the loop, from
        # whatever already existed coming into this call; a credit note
        # created by an earlier iteration of the loop below is tracked
        # separately (credit_note_created), since a plain out_refund
        # already carries no document at all until _meli_relate_invoice_
        # document runs — self.invoice_ids itself needs no re-fetching
        # for that, but relying on it alone would miss a same-call
        # creation if the field were ever cached upstream, so the local
        # flag is the one both branches actually check below.
        existing_live_refund = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
        )
        credit_note_created = self.env['account.move']
        for credit_note_document in credit_note_documents:
            already_related = self.invoice_ids.filtered(
                lambda m: m.move_type == 'out_refund'
                and m.meli_invoice_document_id == credit_note_document
            )
            if already_related:
                continue

            if existing_live_refund or credit_note_created:
                # Critical fix: Mercado Libre can issue a REPLACEMENT
                # devolución CFDI for the same cancellation — a genuinely
                # different meli.invoice.document row (see _meli_upsert's
                # own docstring) that this method would otherwise never
                # recognize as "already handled", since the old check
                # only compared document identity. Creating a SECOND real
                # out_refund against the same invoice lines would be an
                # uncapped double refund — degrade to manual review
                # instead. Auto-superseding the old credit note requires
                # accounting judgment out of scope for this fix.
                self.message_post(body=_(
                    "Mercado Libre generated another credit note "
                    "(%(document)s) for this order, but a credit note "
                    "already exists for it — review manually, this new "
                    "document was not applied automatically."
                ) % {'document': credit_note_document.meli_invoice_id or credit_note_document.id})
                continue

            if not source_invoice:
                self.message_post(body=_(
                    "Mercado Libre generated a credit note (%(document)s) "
                    "for this order, but there is no posted invoice yet to "
                    "apply it to — review manually."
                ) % {'document': credit_note_document.meli_invoice_id or credit_note_document.id})
                continue
            journal = source_invoice.journal_id.filtered('active')
            wizard = self.env['account.move.reversal'].create({
                'move_ids': [(6, 0, source_invoice.ids)],
                'journal_id': journal.id,
                'company_id': source_invoice.company_id.id,
                'reason': _(
                    "Mercado Libre credit note %s"
                ) % (credit_note_document.meli_invoice_id or credit_note_document.id),
            })
            wizard.reverse_moves(is_modify=False)
            credit_note = wizard.new_move_ids
            # Fix 4 (2026-09-09, user-directed follow-up): account.move.
            # reversal has no invoice_date create field of its own — its
            # `date` field only ever drives the new move's accounting
            # `date`, and reverse_moves() otherwise leaves invoice_date to
            # its ordinary default (today, set once action_post() below
            # runs). The credit note must instead carry the REAL Mercado
            # Libre document's own fiscal date — see
            # _meli_credit_note_invoice_date's own docstring for why that
            # needs converting back through MELI_FISCAL_TIMEZONE, not a
            # naive .date() on the stored (UTC) issue_date. Set before
            # action_post(), same as every other business field this
            # method finalizes before posting.
            credit_note_date = self._meli_credit_note_invoice_date(credit_note_document)
            if credit_note_date:
                credit_note.invoice_date = credit_note_date
            credit_note.action_post()
            self._meli_relate_invoice_document(credit_note, credit_note_document)
            credit_note_created = credit_note
            self.message_post(body=_(
                "Credit note %(credit_note)s created and related to "
                "Mercado Libre document %(document)s."
            ) % {'credit_note': credit_note.name,
                 'document': credit_note_document.meli_invoice_id or credit_note_document.id})

    @staticmethod
    def _meli_credit_note_invoice_date(document):
        """Fix 4 (2026-09-09, user-directed follow-up): the credit
        note's own invoice_date must reflect the REAL Mercado Libre
        document — its own issue_date (Datetime, parsed straight from
        the CFDI's own Fecha attribute — see
        meli.invoice.document.issue_date's own help text) — never
        today (the account.move.reversal wizard's own default) and
        never the source invoice's own, unrelated date (the old
        behaviour of the sibling-scoped, hand-built path).

        issue_date is stored as naive UTC (see meli.invoice.document.
        _meli_parse_issue_date_from_xml, which converts FROM
        Monterrey/CDMX local time TO UTC before saving, for exactly the
        reason documented there and at MELI_FISCAL_TIMEZONE's own
        definition above: SAT stamps every CFDI's Fecha in Monterrey/
        CDMX local time). A plain issue_date.date() on that stored UTC
        value would read back the WRONG calendar day for any CFDI
        stamped late at night Monterrey time (e.g. 23:45 local rolls
        into the next UTC day) — converting back through
        MELI_FISCAL_TIMEZONE here undoes that shift and recovers the
        real fiscal date the CFDI itself reports.

        Returns False (never raises) when issue_date isn't set — same
        "missing means not derivable, not a crash" convention
        _meli_parse_issue_date_from_xml itself already uses; callers
        fall back to some other date in that case.
        """
        if not document.issue_date:
            return False
        return (
            pytz.utc.localize(document.issue_date)
            .astimezone(MELI_FISCAL_TIMEZONE)
            .date()
        )

    def _meli_relate_invoice_document(self, move, document):
        """Relates a meli.invoice.document's real XML to an Odoo
        account.move as its CFDI, WITHOUT ever calling Odoo's own PAC —
        reuses the exact method l10n_mx_edi itself calls after a
        successful PAC send (_l10n_mx_edi_cfdi_invoice_document_sent),
        just fed with Mercado Libre's XML instead. This is what makes
        account.move._is_cfdi_issued_by_third_party() stop blocking this
        move afterward (it 'Stays False once the move already has a
        UUID' — see xe_l10n_mx_edi/models/account_move.py): the UUID
        gets computed from the attachment this creates.
        """
        move.ensure_one()
        document.ensure_one()
        xml_bytes = base64.b64decode(document.xml_file)
        filename = document.xml_filename or f"{document.meli_invoice_id or document.id}.xml"
        move._l10n_mx_edi_cfdi_invoice_document_sent(filename, xml_bytes)
        move.meli_invoice_document_id = document.id

    def _meli_relate_partial_cancellation_credit_note(self, lines, cancelled_order_id):
        """Builds (or finds, if a previous call already built it — this
        method is idempotent, same convention as everything else in this
        file) a line-scoped credit note for ONE sibling's own product
        line(s) within a consolidated pack invoice. Called only from
        _meli_process_partial_cancellation — see that method's own
        docstring for why the credit note is related BEFORE the stock
        return.

        Correction 1 (preflight review, 2026-09-08): this deliberately
        does NOT reuse _meli_reconcile_invoicing's own credit-note step
        (account.move.reversal.reverse_moves(is_modify=False)) — that
        wizard mirrors EVERY line of the source invoice into the new
        credit note. For a single, non-pack order that's exactly right
        (a credit note always means the whole thing was returned), but
        for a consolidated pack invoice covering more than one sibling's
        own line — a real, confirmed shape: _create_invoices() (Task 2)
        invoices a whole order by default, with no filtering by
        meli_order_id — a full reversal would credit-note every OTHER
        still-paid sibling's line too, over-refunding and corrupting
        their still-legitimate revenue. So this builds a brand new
        out_refund account.move by hand, with invoice_line_ids copied
        ONLY from the invoice line(s) that actually belong to THIS
        sibling (matched via account.move.line.sale_line_ids, sale's own
        many2many back-reference to sale.order.line — populated by
        _create_invoices() for exactly this purpose).

        Returns the related account.move (out_refund) — either just
        created, or the one already related from a previous, idempotent
        call — or an empty 'account.move' recordset when there's nothing
        to relate yet (no credit-note document from Mercado Libre yet,
        no posted invoice yet to apply it to, or this sibling's own
        product line(s) can't be found on that invoice). Each of those
        "not ready yet" cases posts its own chatter message, mirroring
        _meli_reconcile_invoicing's own credit-note step for the
        whole-order case.

        Final review fix (2026-09-08): applies the exact same three
        fixes as _meli_reconcile_invoicing's own credit-note step — see
        that method's comments for the full rationale of each:
        Fix 2 (xml_file filter — this one already had it), Fix 1
        (excludes rejected/cancelled documents, AND never creates a
        second live out_refund for this sibling's own lines just
        because a newer document arrived), and Fix 3 (iterates every
        actionable document for this sibling oldest-first, instead of
        only ever considering the single newest one).
        """
        self.ensure_one()
        # Local import: see _meli_reconcile_invoicing's own docstring for
        # why this can't be a module-level import (circular import with
        # meli_invoice_document.py).
        from .meli_invoice_document import (
            MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES, MELI_INVOICE_DEAD_STATUSES,
        )

        # Scoped to THIS sibling's own meli_order_id, deliberately NOT to
        # sale_order_id (which _meli_reconcile_invoicing's own search
        # uses): a pack's consolidated sale_order_id is shared by every
        # sibling, so searching by it alone would risk picking up a
        # credit note actually meant for a DIFFERENT sibling.
        # meli_order_id is the one field a document always carries for
        # exactly the individual order it was issued against. No
        # `limit=1` and ordered oldest-first — see _meli_reconcile_
        # invoicing's own comment on its equivalent search for why
        # (Fix 3): an older, never-yet-related document for this same
        # sibling must not be permanently masked by a newer one.
        credit_note_documents = self.env['meli.invoice.document'].sudo().search([
            ('meli_order_id', '=', cancelled_order_id),
            ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
            ('xml_file', '!=', False),
            ('status', 'not in', list(MELI_INVOICE_DEAD_STATUSES)),
        ], order='create_date asc, id asc')
        if not credit_note_documents:
            return self.env['account.move']

        source_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        )[:1]
        # Fix 1: whether THIS sibling's own invoice line(s) are already
        # covered by a live (non-cancelled) out_refund — regardless of
        # which meli.invoice.document row that refund is related to.
        # Computed from whatever refund line(s) already exist coming
        # into this call; a credit note created by an earlier iteration
        # of the loop below is tracked separately (credit_note_created).
        existing_live_refund_lines = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
        ).invoice_line_ids.filtered(lambda l: l.sale_line_ids & lines)
        credit_note_created = self.env['account.move']

        for credit_note_document in credit_note_documents:
            already_related = self.invoice_ids.filtered(
                lambda m: m.move_type == 'out_refund'
                and m.meli_invoice_document_id == credit_note_document
            )
            if already_related:
                credit_note_created = credit_note_created or already_related[:1]
                continue

            if existing_live_refund_lines or credit_note_created:
                # Critical fix: same double-refund hazard as
                # _meli_reconcile_invoicing's own whole-order path — see
                # that method's comment on this exact check for the full
                # rationale (a genuine REPLACEMENT devolución document,
                # confirmed possible by _meli_upsert's own docstring,
                # must never create a SECOND real out_refund for lines
                # already covered).
                self.message_post(body=_(
                    "Mercado Libre generated another credit note "
                    "(%(document)s) for order %(order_id)s (one "
                    "individual order within this pack), but a credit "
                    "note already covers its own line(s) — review "
                    "manually, this new document was not applied "
                    "automatically."
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'order_id': cancelled_order_id,
                })
                continue

            if not source_invoice:
                self.message_post(body=_(
                    "Mercado Libre generated a credit note (%(document)s) for "
                    "order %(order_id)s (one individual order within this "
                    "pack), but there is no posted invoice yet to apply it "
                    "to — review manually."
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'order_id': cancelled_order_id,
                })
                continue

            matching_invoice_lines = source_invoice.invoice_line_ids.filtered(
                lambda l: l.sale_line_ids & lines
            )
            if not matching_invoice_lines:
                self.message_post(body=_(
                    "Mercado Libre generated a credit note (%(document)s) for "
                    "order %(order_id)s (one individual order within this "
                    "pack), but its product line(s) could not be found on "
                    "invoice %(invoice)s — review manually."
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'order_id': cancelled_order_id,
                    'invoice': source_invoice.name,
                })
                continue

            # with_context(include_business_fields=True) is the exact same
            # flag account.move._reverse_moves itself sets before calling
            # copy() — it's what makes sale's own account.move.line.
            # _copy_data_extend_business_fields() copy sale_line_ids onto
            # the new line despite that field being copy=False by default
            # (a PLAIN duplicate of an invoice line should never claim to
            # invoice the same sale line again — but this credit note IS a
            # genuine, deliberate reuse of that exact same portion, so the
            # same override Odoo's own reversal machinery relies on applies
            # here too). Only the matched line(s) are copied — never the
            # invoice's other lines (a sibling's own product, or any
            # global rounding/discount line) — which is the entire point of
            # building this move by hand instead of reusing
            # account.move.reversal's whole-move copy.
            line_vals_list = matching_invoice_lines.with_context(
                include_business_fields=True,
            ).copy_data()
            for vals in line_vals_list:
                vals.pop('move_id', None)

            credit_note = self.env['account.move'].create({
                'move_type': 'out_refund',
                'reversed_entry_id': source_invoice.id,
                'partner_id': source_invoice.partner_id.id,
                'currency_id': source_invoice.currency_id.id,
                'company_id': source_invoice.company_id.id,
                # Fix 4 (2026-09-09, user-directed follow-up): dated from
                # the real Mercado Libre document's own fiscal date, not
                # copied from source_invoice's own (unrelated) invoice
                # date — see _meli_credit_note_invoice_date's own
                # docstring. Falls back to source_invoice's own date only
                # in the (never actually seen) case issue_date wasn't
                # parseable, so this never regresses to a blank date.
                'invoice_date': (
                    self._meli_credit_note_invoice_date(credit_note_document)
                    or source_invoice.invoice_date
                ),
                'journal_id': source_invoice.journal_id.id,
                'invoice_origin': source_invoice.invoice_origin,
                'ref': _(
                    "Mercado Libre credit note %(document)s (order %(order_id)s)"
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'order_id': cancelled_order_id,
                },
                'invoice_line_ids': [(0, 0, vals) for vals in line_vals_list],
            })
            credit_note.action_post()
            self._meli_relate_invoice_document(credit_note, credit_note_document)
            credit_note_created = credit_note

        return credit_note_created

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

    @api.model
    def _meli_create_from_order_data(self, config, order_data):
        order_id = str(order_data.get('id'))
        existing = self.search([('meli_order_id', '=', order_id)], limit=1)
        if existing:
            return existing

        # pack_id has to be known before the 'not paid yet' check below:
        # a status-change notification for a sibling order (cancelled,
        # pending_cancel, partially_refunded — see
        # MELI_STATUS_CHANGE_ALERTS) never reports 'paid' again, so it
        # used to be silently swallowed by that check with a misleading
        # "skipping import" log line and never reached
        # _meli_flag_status_change at all — before this module
        # consolidated pack siblings into one sale.order, each sibling
        # had its own order/sale.order and would have gotten this
        # notification normally. A consolidated pack sale.order still
        # only acts on the WHOLE order (per-sibling/per-line granularity
        # is spec section 5, explicitly paused) — this only restores the
        # manual-review chatter visibility a non-pack order already had.
        pack_id = str(order_data.get('pack_id') or '') or False

        # Adopt a sale.order some OTHER system (Ventiapp, this
        # connector's predecessor) already created for this same real
        # Mercado Libre order/pack, instead of creating a duplicate.
        # Ventiapp's own convention (confirmed 2026-09-01, matched by
        # this connector's own customer_ref below) sets reference/
        # client_order_ref to the pack id when the order is part of a
        # pack, or the plain order id otherwise — but Ventiapp never
        # populates meli_sync_source/meli_order_id, since those fields
        # belong to this connector alone. 652,874 such orders exist in
        # production (2026-09-09 investigation) — a duplicate reference
        # is a real, confirmed contributor to the "OC del cliente ya
        # existe" crash from ir.actions.server id 679 (never touch that
        # automation — see Global Constraints), and creating a second
        # sale.order for a real transaction Ventiapp already recorded is
        # wrong regardless of that crash. meli_sync_source = False scopes
        # this to orders THIS connector never touched — an order this
        # connector already created always has meli_sync_source set, so
        # it's already covered by the meli_order_id dedup above and must
        # never be re-matched here.
        adoption_ref = pack_id or order_id
        # Fix 3 (2026-09-09, final review — Important): ('state', '!=',
        # 'cancel') added — an ALREADY-CANCELLED Ventiapp order sharing
        # this reference must never be adopted; that would silently
        # adopt the wrong, dead record and lose track of what should be
        # a real, separate paid sale. No `limit` here (was limit=1
        # before this fix): Ventiapp can have MULTIPLE sibling
        # sale.orders sharing one `reference` value for a genuine,
        # not-yet-consolidated pack (see the comment on customer_ref
        # below) — adopting an arbitrary one of several candidates would
        # corrupt the rest of the pack-sibling resolution logic for
        # later notifications, so that case is branched on explicitly
        # below instead of ever being silently picked by `limit`.
        adoption_candidates = self.search([
            ('reference', '=', adoption_ref),
            ('meli_sync_source', '=', False),
            ('state', '!=', 'cancel'),
        ])
        adoption_ambiguous_count = 0
        if len(adoption_candidates) == 1:
            adoptable = adoption_candidates
            adoptable.write({
                'meli_sync_source': 'xe_meli_connector',
                'meli_order_id': order_id,
                'meli_pack_id': pack_id,
                'meli_adopted': True,
                'meli_order_date_created': self._meli_parse_datetime(
                    order_data.get('date_created'),
                ),
                'meli_order_date_closed': self._meli_parse_datetime(
                    order_data.get('date_closed'),
                ),
            })
            adoptable._meli_post_with_mention(_(
                "This sale was originally created outside this "
                "connector (Mercado Libre order/pack %s) and has now "
                "been linked here — future updates (invoicing, "
                "cancellations) will be handled automatically from "
                "this point forward. Its own commercial details "
                "(lines, pricing, customer, warehouse) were left "
                "untouched."
            ) % adoption_ref)
            # Fix 2 (2026-09-09, final review — Important): the
            # notification that triggered this adoption doesn't
            # necessarily report 'paid' (e.g. a Ventiapp order first
            # seen here via a 'cancelled' notification) — route it
            # through the normal status-change handling, mirroring the
            # pack-sibling "not paid yet" branch just below. Without
            # this, the real status-change logic
            # (_meli_flag_status_change) never runs for that
            # notification at all, and — for a non-pack order — its own
            # same-status dedup could then suppress an identical, later
            # notification forever (meli_last_status would never have
            # been recorded here). Deliberately does NOT also set
            # meli_last_status directly in the write() above: it's left
            # at its prior value (False — this connector has never
            # touched this order before) so
            # _meli_flag_status_change's own dedup check (new_status ==
            # self.meli_last_status) can't short-circuit before it even
            # runs — that method sets meli_last_status itself, right
            # after that check passes. When the status IS 'paid',
            # adoption alone is the correct, complete response, so
            # meli_last_status is simply recorded directly instead.
            if order_data.get('status') != 'paid':
                adoptable._meli_flag_status_change(order_data)
            else:
                adoptable.meli_last_status = order_data.get('status')
            return adoptable
        elif len(adoption_candidates) > 1:
            # Ambiguous: several existing Ventiapp orders share this
            # exact reference (an un-consolidated pack). Adopting an
            # arbitrary one would corrupt pack-sibling resolution for
            # later notifications, so none of them is adopted — normal
            # order creation proceeds below instead, and the ambiguity
            # is flagged on the NEW order for a human to reconcile (see
            # the _meli_post_with_mention call once `order` exists,
            # further down this method).
            adoption_ambiguous_count = len(adoption_candidates)

        if order_data.get('status') != 'paid' and pack_id:
            pack_order = self.sudo().search([('meli_pack_id', '=', pack_id)], limit=1)
            if pack_order:
                pack_order._meli_flag_status_change(order_data)
                return pack_order

        new_status = order_data.get('status')
        if new_status not in ('paid', 'cancelled'):
            _logger.info(
                "Mercado Libre order %s has status '%s' (not 'paid' yet), "
                "skipping import.", order_id, new_status,
            )
            return self.browse()

        if not config.partner_id:
            raise UserError(_(
                "Configure the 'Mercado Libre Customer' on the connection "
                "(Mercado Libre > Settings) before importing sales."
            ))

        # Fix 2026-09-10: ONE shared fetch of /orders/{id}/shipments for
        # both the logistic type (below) and the custom-shipping surcharge
        # (further down) — see _meli_fetch_shipment_records's own
        # docstring for why the previous two-independent-calls design was
        # reversed (it caused a real lost freight surcharge). A failure
        # here is retried as a whole (RetryableJobError) rather than
        # creating the order without knowing its shipping details —
        # this order's fiscal/logistics facts must be certain before it
        # exists at all.
        shipping_id_for_shipments = (order_data.get('shipping') or {}).get('id')
        if shipping_id_for_shipments:
            try:
                shipment_records = self._meli_fetch_shipment_records(
                    config, order_id, order_data,
                )
            except requests.exceptions.RequestException as err:
                raise RetryableJobError(
                    f"Could not fetch Mercado Libre shipping details for "
                    f"order {order_id} — will retry.", seconds=30,
                ) from err
        else:
            shipment_records = []

        logistic_type = self._meli_fetch_logistic_type(
            config, order_id, order_data, shipments=shipment_records,
        )
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

        # Recovery: an order whose very first-ever reported status is
        # 'cancelled' (never seen 'paid' by this connector) is normally
        # never created at all — see the `new_status not in ('paid',
        # 'cancelled')` check above. For Full, only recover it when
        # Mercado Libre already issued at least one real invoice/credit-
        # note document for it (nothing to reconcile fiscally otherwise);
        # for non-Full, always recover as far as create+confirm (see
        # docs/superpowers/specs/2026-09-09-meli-cancelled-order-recovery-design.md).
        # Searched by meli_order_id, trying the pack id first when this
        # order is part of one — confirmed with the user that Mercado
        # Libre invoices are filed under the pack id.
        if new_status == 'cancelled' and is_fulfillment:
            has_existing_documents = bool(
                self.env['meli.invoice.document'].sudo().search([
                    ('meli_order_id', 'in', [pack_id, order_id] if pack_id else [order_id]),
                ], limit=1)
            )
            if not has_existing_documents:
                _logger.info(
                    "Mercado Libre order %s is 'cancelled' and Full, "
                    "with no invoice/credit-note document yet — "
                    "skipping import.", order_id,
                )
                return self.browse()

        if pack_id:
            existing_line = self.env['sale.order.line'].sudo().search(
                [('meli_order_id', '=', order_id)], limit=1,
            )
            if existing_line:
                return existing_line.order_id
            pack_order = self.sudo().search(
                [('meli_pack_id', '=', pack_id)], limit=1,
            )
            if pack_order:
                # Fix B (2026-09-09, re-review of the final-review fix
                # round — Important): an ADOPTED order can match here
                # (adoption sets meli_pack_id when the adoption
                # reference was itself a pack id — see
                # _meli_create_from_order_data's adoption branch above),
                # and _meli_add_pack_sibling_lines silently adds new
                # commercial order lines (and can auto-validate a
                # delivery for a Full order) — exactly what the
                # adoption's own chatter message promises will never
                # happen. Mirrors the ambiguous-adoption-candidates
                # message above: something real was found but not
                # automated on, so it's flagged for a human instead.
                if pack_order.meli_adopted:
                    pack_order._meli_post_with_mention(_(
                        "A new Mercado Libre order (%(order_id)s) "
                        "arrived for this pack, but was not "
                        "automatically added: this sale was adopted "
                        "from another system, so its commercial "
                        "details are left untouched. Review manually "
                        "whether/how to add it."
                    ) % {'order_id': order_id})
                    return pack_order
                return pack_order._meli_add_pack_sibling_lines(order_data, order_id)

        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data, order_id)

        # Shares shipment_records (fetched once, above) — no separate
        # HTTP call and no separate failure mode: a fetch failure was
        # already raised as RetryableJobError before this order's data
        # was even built (see shipment_records above).
        custom_shipping_cost = self._meli_fetch_custom_shipping_cost(
            config, order_id, order_data, shipments=shipment_records,
        )
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
        if adoption_ambiguous_count:
            # Fix 3 (2026-09-09, final review — Important): flags the
            # ambiguity found earlier (several existing Ventiapp orders
            # shared this exact reference, so none of them was adopted)
            # on the newly-created order, naming both the count and the
            # reference value so a human can go reconcile the duplicates
            # manually.
            order._meli_post_with_mention(_(
                "%(count)s existing Ventiapp order(s) already share "
                "reference %(reference)s — too ambiguous to adopt "
                "automatically, so this sale was created as a new "
                "order instead. Please reconcile these manually."
            ) % {'count': adoption_ambiguous_count, 'reference': adoption_ref})
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
            # Isolated: a failure in action_confirm() (this database has
            # 16 active base_automation rules on sale.order, any of which
            # could raise here) must never propagate out of this method —
            # that would roll back this WHOLE job's savepoint, including
            # the order.create() above (confirmed by reading
            # queue_job_cron_jobrunner's own _process(), which wraps the
            # entire job.perform() call in one savepoint). The order
            # stays alive, in draft, flagged for a human instead.
            try:
                with self.env.cr.savepoint():
                    order.action_confirm()
            except Exception as err:
                _logger.exception(
                    "Mercado Libre order %s: action_confirm() failed — "
                    "left in draft for manual review.",
                    order.client_order_ref,
                )
                order._meli_post_with_mention(_(
                    "This sale could not be confirmed automatically: "
                    "%s. Please review and confirm it manually."
                ) % str(err))
        elif order.state == 'cancel':
            order._meli_post_with_mention(
                _(
                    "The order was created, but something in this "
                    "database cancelled it automatically before we could "
                    "confirm it. Please review manually."
                )
            )
        if order.state == 'sale':
            # Covers both this method's own successful action_confirm()
            # above AND the case where the order was ALREADY 'sale' when
            # we got here (some other automation moved it) — either way,
            # the delivery must exist. See _meli_ensure_delivery's own
            # docstring for why.
            order._meli_ensure_delivery(is_fulfillment)
            if new_status == 'cancelled' and is_fulfillment and order.state == 'sale':
                order._meli_recover_cancelled_on_arrival_full(config)
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

    def _meli_add_pack_sibling_lines(self, order_data, order_id):
        """Adds this individual Mercado Libre order's own product lines
        to an ALREADY-EXISTING pack sale.order, instead of creating a
        second, separate sale.order for it — matching the real,
        confirmed production behaviour (2026-09-07, pack 2000014915601055
        / sale S959129: one sale.order, lines added one at a time as each
        sibling order arrives). Runs regardless of this sale.order's
        state (the real example added a line to an already-confirmed
        'sale' order) and regardless of whether it already has a posted
        invoice — in that case the line is still added, with an extra
        chatter note flagging that the missing amount may need manual
        invoicing (2026-09-07 decision — never block).

        For a Full order, a new line on an already-confirmed order
        triggers Odoo core's own stock-rule logic and creates a brand
        new, pending stock.picking for just that line — nobody at this
        company ever touches a Full transfer manually (see
        _meli_auto_validate_full_pickings), so it has to be
        auto-validated here too, exactly like the very first sibling's
        own pickings are when the order is first created (final branch
        review, 2026-09-07, finding C2).

        If every SKU in this sibling is unmapped, no line gets added at
        all, and none of this module's usual recovery mechanisms
        (_meli_retry_unmapped_lines, the "Retry SKU mapping" button, the
        polling window) help — they all key off self.meli_order_id,
        which is the FIRST sibling's id, not this one's. The only way
        to recover today is to re-import THIS order_id via Mercado
        Libre > Manual Import, which the chatter message below now says
        explicitly. A re-delivered notification for this same order_id
        will keep repeating this same message every time (there is no
        line yet for the idempotency guard to key on) — expected, not a
        bug.

        Fix 2026-09-09 (user-directed follow-up): when THIS sibling
        goes from zero lines to genuinely getting one here — i.e. this
        exact "SKU was unmapped, now it's mapped, re-run the import"
        recovery — meli_pack_has_unmapped_sibling_cancellation is
        cleared back to False, if it was set. See that field's own
        help text.
        """
        self.ensure_one()
        # Fix 2026-09-09 (user-directed follow-up): read BEFORE the
        # write() below, and scoped to THIS specific sibling — this
        # order may already have other lines (its own, or other
        # siblings'), so "did this call add any line at all" isn't the
        # right question; only "did THIS sibling go from zero lines to
        # having one" identifies the exact
        # meli_pack_has_unmapped_sibling_cancellation recovery scenario
        # (see that field's own help text).
        had_no_lines_before = not self.order_line.filtered(
            lambda line: line.meli_order_id == order_id
        )
        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data, order_id)
        line_vals = [command for command, _debug in resolved_lines]
        if line_vals:
            self.write({'order_line': line_vals})
            new_product_ids = {debug['product_id'] for _c, debug in resolved_lines}
            new_lines = self.order_line.filtered(
                lambda line: line.meli_order_id == order_id
                and line.product_id.id in new_product_ids
            )
            price_debug = [debug for _c, debug in resolved_lines]
            self._meli_force_line_prices(new_lines, price_debug)
            for _command, debug in resolved_lines:
                self.message_post(body=_(
                    "Extra line with [%(sku)s] %(name)s (Mercado Libre "
                    "order %(order_id)s, same pack)."
                ) % {
                    'sku': debug['sku'],
                    'name': self.env['product.product'].browse(debug['product_id']).display_name,
                    'order_id': order_id,
                })
            if had_no_lines_before and self.meli_pack_has_unmapped_sibling_cancellation:
                # This sibling previously had ZERO lines of its own —
                # exactly the scenario
                # _meli_notify_zero_line_sibling_cancellation guards
                # against by setting
                # meli_pack_has_unmapped_sibling_cancellation. Now that
                # its SKU is mapped and it finally has a real line, its
                # own status is no longer unconfirmed — from here on it
                # goes through the completely normal
                # partial-cancellation flow, like any other sibling, so
                # the flag no longer needs to hold
                # _meli_close_pack_if_every_sibling_cancelled back.
                # Deliberately does NOT also re-trigger
                # _meli_close_pack_if_every_sibling_cancelled here — the
                # next real cancellation notification or
                # picking-completion event will naturally re-evaluate
                # pack closure, now correctly unblocked.
                self.meli_pack_has_unmapped_sibling_cancellation = False
            if 'MLF' in (self.origin or ''):
                # Fix 8 (2026-09-09, final review — Minor): _meli_ensure_
                # delivery(True) instead of calling
                # _meli_auto_validate_full_pickings() directly — this call
                # site is already confirmed Full-only by the 'MLF' in
                # origin check above, so passing True is correct. A new
                # line on an already-confirmed order can, in principle,
                # land with no picking at all if some other automation
                # interfered — _meli_ensure_delivery's own "make sure a
                # picking exists at all" half (Task 2) is a strict
                # superset of the old "just validate it" behaviour, so
                # this is safe to substitute directly.
                self._meli_ensure_delivery(True)
            if self.invoice_ids.filtered(lambda inv: inv.state == 'posted'):
                self.message_post(body=_(
                    "This order's invoice is already posted — the new line "
                    "above may need manual invoicing."
                ))
        if unmapped_skus:
            self._meli_post_with_mention(
                _(
                    "No Mercado Libre SKU mapping found for: %(skus)s "
                    "(order %(order_id)s, same pack). After adding the "
                    "mapping, re-import order %(order_id)s from Mercado "
                    "Libre > Manual Import to add its line."
                ) % {'skus': ', '.join(unmapped_skus), 'order_id': order_id}
            )
        return self

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
    def _meli_find_order_by_id_or_pack(self, meli_id):
        """Same 4-tier fallback already used ad hoc by meli.claim and
        meli.invoice.document's own _compute_sale_order_id: exact
        meli_order_id, then sale.order.line.meli_order_id (a pack
        sibling other than the first one — after consolidation, its own
        order id only lives on the lines it added, never on the
        sale.order itself, see sale.order.line.meli_order_id), then
        client_order_ref/reference (legacy Ventiapp orders never set
        meli_order_id), then meli_pack_id (meli_id is actually a pack
        id, or belongs to a sibling order already consolidated under
        that pack). Returns an empty recordset if nothing matches any
        of the four.
        """
        order = self.search([('meli_order_id', '=', meli_id)], limit=1)
        if not order:
            line = self.env['sale.order.line'].sudo().search(
                [('meli_order_id', '=', meli_id)], limit=1,
            )
            order = line.order_id
        if not order:
            order = self.search([
                '|', ('client_order_ref', '=', meli_id), ('reference', '=', meli_id),
            ], limit=1)
        if not order:
            order = self.search([('meli_pack_id', '=', meli_id)], limit=1)
        return order

    @api.model
    def _meli_fetch_shipment_records(self, config, order_id, order_data):
        """Single source of truth for GET /orders/{id}/shipments
        (X-New-Domain: true) — shared by _meli_fetch_logistic_type and
        _meli_fetch_custom_shipping_cost.

        Fix 2026-09-10: those two used to each make their OWN,
        independent HTTP call to this exact same endpoint for every
        order import. A transient failure on either one alone (both
        equally likely, same resource, seconds apart) surfaced as "could
        not verify the Mercado Libre shipping mode" on ANY order —
        including plain warehouse-fulfilled ones that were never custom
        shipping to begin with, purely because the SECOND of the two
        redundant calls happened to be the unlucky one. Confirmed in
        production: a genuinely custom-shipping order lost its freight
        surcharge this way, with only that one warning left in the
        chatter as a trace. One shared fetch removes the race entirely
        (a caller that already knows about the earlier "deliberately
        separate calls" plan should treat this as its explicit reversal
        — the duplicate-traffic and false-alarm cost turned out to
        outweigh whatever kept them apart).

        Returns [] when the order has no shipping id at all (nothing to
        fetch — never a failure). Raises
        requests.exceptions.RequestException on a genuine fetch failure;
        the caller decides what that means.
        """
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return []
        shipments = config._api_get(
            f'/orders/{order_id}/shipments',
            headers={'X-New-Domain': 'true'},
        )
        if isinstance(shipments, dict):
            shipments = [shipments]
        return shipments or []

    @api.model
    def _meli_fetch_logistic_type(self, config, order_id, order_data, shipments=None):
        """The order resource only carries a shipping id, not the
        logistic_type — a separate call to /orders/$ID/shipments is
        required. Falls back to None (non-fulfillment) on any failure so a
        transient issue here doesn't block the whole sale from being
        created.

        `shipments`, when given (a list, possibly empty — see
        _meli_fetch_shipment_records), skips the HTTP fetch entirely and
        parses from it directly. _meli_create_from_order_data always
        passes it, having already fetched shipments once for both this
        and _meli_fetch_custom_shipping_cost to share (2026-09-10 fix).
        Left as an internal fallback fetch for any other/future caller
        that still wants this method's original do-it-all behaviour.
        """
        if shipments is None:
            shipping_id = (order_data.get('shipping') or {}).get('id')
            if not shipping_id:
                return None
            try:
                shipments = self._meli_fetch_shipment_records(config, order_id, order_data)
            except requests.exceptions.RequestException:
                _logger.warning(
                    "Could not fetch shipments for Mercado Libre order %s, "
                    "defaulting to the non-fulfillment warehouse.", order_id,
                )
                return None
        for shipment in shipments or []:
            if shipment.get('type') == 'forward':
                return shipment.get('logistic_type')
        return None

    @api.model
    def _meli_fetch_custom_shipping_cost(self, config, order_id, order_data, shipments=None):
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

        `shipments`, when given, skips the HTTP fetch — see
        _meli_fetch_logistic_type's docstring for why
        (_meli_fetch_shipment_records is now the one shared fetch;
        2026-09-10 fix, reversing the earlier "deliberately separate
        calls" decision after it caused a real lost freight surcharge).

        Unlike _meli_fetch_logistic_type, a RequestException from the
        internal fallback fetch here is deliberately NOT swallowed — it
        propagates to the caller. This method's None return value
        already means something specific ("not custom shipping"), and
        silently reusing it for "the fetch failed" made a failed fetch
        indistinguishable from a genuinely non-custom order: the order
        got created and confirmed with no surcharge line and nothing
        looked wrong (found in the final branch review, 2026-08-31).
        _meli_create_from_order_data is the one that decides what a
        failure here should mean.
        """
        if shipments is None:
            shipping_id = (order_data.get('shipping') or {}).get('id')
            if not shipping_id:
                return None
            shipments = self._meli_fetch_shipment_records(config, order_id, order_data)
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
    def _meli_build_order_lines(self, order_data, order_id):
        """Returns (resolved_lines, unmapped_skus). Each entry in
        resolved_lines is (create_command, debug_dict) — the debug_dict
        carries the raw Mercado Libre price alongside the computed
        price_unit, so callers can force the real price back onto the
        line after creation (see _meli_force_line_prices). Every line
        command stamps meli_order_id so it can always be traced back to
        the individual Mercado Libre order that added it, even when
        several individual orders share one sale.order (a pack).
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
                    'meli_order_id': order_id,
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


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    meli_order_id = fields.Char(
        string='Mercado Libre Order ID', copy=False,
        help="The individual Mercado Libre order id that added this "
             "specific line. Always set for a line created from a "
             "Mercado Libre import — a pack order's sale.order can carry "
             "lines from several different individual orders, each "
             "sharing the same 'OC Cliente' (see sale.order.meli_pack_id) "
             "but each keeping its own order id here.",
    )
