import base64
import logging
import re
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_is_zero

import dateutil.parser
import pytz
import requests
from psycopg2 import OperationalError

from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY

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

# Fix 2026-09-14 (user request): _meli_import_order is this trimmed,
# invoicing-only module's ONLY remaining way to auto-create a sale order
# from Mercado Libre data — reached exclusively from meli.invoice.
# document._meli_upsert's Trigger B (an invoice/credit-note arrived
# orphaned). For a genuinely recent order, Ventiapp (the separate,
# external system that normally injects these) hasn't necessarily had
# its own chance yet — racing ahead and creating it here first would
# permanently deny that order the "adopted" treatment (Ventiapp's own
# commercial details preserved, this connector only takes over the
# fiscal side going forward — see _meli_create_from_order_data's own
# adoption-matching). Orders older than this grace period get no delay
# at all: if Ventiapp hasn't injected it by then, it never will.
MELI_ORDER_RECOVERY_GRACE_MINUTES = 15

# Fix 2026-09-22 (user decision, re-enabling the sale.order injector):
# a sale.order created by user id 8 (Horacio González Montfort) for
# partner_id 87659 (MERCADO LIBRE) is a manual/internal record, never a
# real order VentiApp mirrored from Mercado Libre — adopting one of
# these (see _meli_create_from_order_data's own adoption branch) would
# silently hijack it and could duplicate the real Mercado Libre order
# that happens to share its reference. Excluded from every adoption
# match; this connector must never touch, adopt, or import over one.
# Kept as a defense-in-depth safety net for any such order that
# already existed before the hard block below (SaleOrder._check_meli_
# horacio_cannot_use_mercado_libre_partner) started actually
# preventing this combination from ever being created at all.
MELI_ADOPTION_EXCLUDED_CREATE_UID = 8
MELI_ADOPTION_EXCLUDED_PARTNER_ID = 87659


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
    meli_auto_recovered = fields.Boolean(
        string='Meli: Auto-Recovered (No VentiApp)', default=False, copy=False,
        help="Set when this order was created by _meli_import_order's "
             "Trigger B (an invoice/credit-note document arrived orphaned "
             "— see meli.invoice.document._meli_upsert) WITHOUT finding an "
             "adoptable Ventiapp order first — i.e. Ventiapp never "
             "injected this sale within its own grace period "
             "(MELI_ORDER_RECOVERY_GRACE_MINUTES), so this connector "
             "created it directly. Never set on an adopted order "
             "(meli_adopted=True already covers that case) or on one "
             "Ventiapp had already created — this flags the specific, "
             "worth-reviewing case of a sale this connector had to "
             "invent on its own.",
    )
    meli_last_status = fields.Char(
        string='Last Mercado Libre Status', copy=False,
        help="Last status of the order as reported by Mercado Libre. Used "
             "only to detect changes (e.g. cancellations) — never "
             "triggers any automatic action in Odoo.",
    )
    meli_pack_had_sibling_added = fields.Boolean(
        string='Product Added After Sale Existed', default=False, copy=False,
        help="Checked when this sale already existed and a product from "
             "the same Mercado Libre pack was added to it afterward. "
             "Double-check that this sale's invoice, and credit note if "
             "it has one, are correct.",
    )
    meli_has_unapplied_document = fields.Boolean(
        string='Has an Unapplied Mercado Libre Document',
        compute='_compute_meli_has_unapplied_document',
        help="True when at least one meli.invoice.document already "
             "related to this sale (sale_order_id set) was never "
             "actually applied (is_applied still False — see that "
             "field's own help text) and isn't a partial-refund credit "
             "note deliberately paused for manual handling "
             "(meli_needs_manual_credit_note). Only ever used to hide "
             "the 'Retry Invoicing' button once there's genuinely "
             "nothing left for it to do — a paused document is not "
             "stuck, and retrying it would just repeat the same "
             "manual-review chatter message.",
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
                ('meli_needs_manual_credit_note', '=', False),
            ]))

    @api.constrains('partner_id')
    def _check_meli_horacio_cannot_use_mercado_libre_partner(self):
        """Fix 2026-09-22 (user decision, hard block — no exceptions "for
        UI or code/server actions"): a sale created by user id 8
        (Horacio González Montfort) for partner_id 87659 (MERCADO
        LIBRE) must never be possible to create/save at all, not just
        silently ignored afterward by this connector's own adoption
        logic (see MELI_ADOPTION_EXCLUDED_CREATE_UID/_PARTNER_ID's own
        comment — that exclusion stays as a safety net for anything
        that already existed before this constraint did).

        Checked against self.env.uid — the user actually executing this
        ORM call right now — not self.create_uid: a plain create_uid
        check would miss a write() that changes partner_id to Mercado
        Libre afterward, and would also miss (rather than block) a
        deliberate .sudo().with_user(8) call, which sets env.uid to 8
        exactly the same way a real session as that user would. This
        connector's own automated order creation never runs as user 8,
        so this can never block a genuine Mercado Libre import.
        """
        for order in self:
            if (
                self.env.uid == MELI_ADOPTION_EXCLUDED_CREATE_UID
                and order.partner_id.id == MELI_ADOPTION_EXCLUDED_PARTNER_ID
            ):
                raise UserError(_(
                    "Este usuario no puede crear ni guardar ventas para "
                    "el cliente Mercado Libre — esas ventas solo debe "
                    "generarlas el conector de Mercado Libre."
                ))
    meli_invoice_document_ids = fields.One2many(
        'meli.invoice.document', 'sale_order_id',
        string='Mercado Libre Invoicing Documents',
    )
    meli_has_refacturacion = fields.Boolean(
        string='Refactured (Global + Nominal)',
        compute='_compute_meli_invoice_flags', store=True,
        help="True when this order has more than one 'factura' document "
             "(document_type='factura') — the normal signature of a "
             "Mercado Libre refacturación (re-invoicing): the original "
             "Factura Global gets superseded by a Factura Nominal once "
             "the buyer requests one with their own RFC. The older "
             "document is kept (usually cancelled), never deleted.",
    )
    meli_has_credit_note = fields.Boolean(
        string='Has Credit Note',
        compute='_compute_meli_invoice_flags', store=True,
        help="True when this order has at least one live "
             "(document_type='nota_de_credito', status not "
             "rejected/cancelled) credit note — e.g. a return.",
    )
    meli_shipping_scheme = fields.Selection([
        ('full', 'Full'),
        ('traditional', 'Traditional (Mercado Envíos)'),
        ('custom', 'Custom'),
    ], string='Mercado Libre Shipping Scheme', copy=False,
        help="Which shipping scheme Mercado Libre reports for this "
             "order: Full (fulfilled from Mercado Libre's own "
             "warehouse), Traditional (ordinary Mercado Envíos, mode "
             "'me2'), or Custom (the seller manages the courier "
             "directly, mode 'custom'). Set once at import time from "
             "the order's own shipment data; empty when the order has "
             "no shipment at all.",
    )
    meli_transaction_type = fields.Selection([
        ('sale', 'Sale'),
        ('resale', 'Resale'),
    ], string='Mercado Libre Transaction Type',
        compute='_compute_meli_invoice_flags', store=True,
        help="Whether Mercado Libre's own invoice for this sale is "
             "'sale' (a normal sale) or 'resale' (catalog/buybox) — "
             "read from meli.invoice.document.transaction_type. Empty "
             "until this order's own invoice document arrives (nothing "
             "on the order resource itself says this ahead of time).",
    )
    # 2026-09-21 (user request): order-level totals of the same two
    # per-line fields (sale.order.line.meli_discount_amount/meli_
    # discount_ml_funded_amount) — see those fields' own help text.
    # Kept here too, summed, so "how much did coupons cost me across my
    # sales, and did Mercado Libre ever cover part of it" is visible at
    # a glance across many orders, without opening each one's lines.
    meli_discount_amount = fields.Monetary(
        string='Mercado Libre Discount', compute='_compute_meli_discount_amounts',
        store=True, currency_field='currency_id',
        help="Total coupon/promotion discount Mercado Libre applied "
             "across every line of this sale (sale.order.line.meli_"
             "discount_amount, summed). 0 when no line had a discount. "
             "Informational only — never affects amount_total.",
    )
    meli_discount_ml_funded_amount = fields.Monetary(
        string='Mercado Libre-Funded Discount', compute='_compute_meli_discount_amounts',
        store=True, currency_field='currency_id',
        help="Of meli_discount_amount, the portion Mercado Libre itself "
             "(or a brand/campaign) funded rather than XE (sale.order."
             "line.meli_discount_ml_funded_amount, summed). 0 whenever "
             "XE funded the whole discount itself.",
    )

    @api.depends(
        'order_line.meli_discount_amount', 'order_line.meli_discount_ml_funded_amount',
    )
    def _compute_meli_discount_amounts(self):
        for order in self:
            order.meli_discount_amount = sum(order.order_line.mapped('meli_discount_amount'))
            order.meli_discount_ml_funded_amount = sum(
                order.order_line.mapped('meli_discount_ml_funded_amount')
            )

    @api.depends(
        'meli_invoice_document_ids.document_type',
        'meli_invoice_document_ids.status',
        'meli_invoice_document_ids.transaction_type',
    )
    def _compute_meli_invoice_flags(self):
        from .meli_invoice_document import MELI_INVOICE_DEAD_STATUSES
        for order in self:
            documents = order.meli_invoice_document_ids
            facturas = documents.filtered(
                lambda d: d.document_type == 'factura'
            )
            notas = documents.filtered(
                lambda d: d.document_type == 'nota_de_credito'
                and d.status not in MELI_INVOICE_DEAD_STATUSES
            )
            order.meli_has_refacturacion = len(facturas) > 1
            order.meli_has_credit_note = bool(notas)
            sale_or_resale = facturas.filtered(
                lambda d: d.transaction_type in ('sale', 'resale')
            )
            order.meli_transaction_type = sale_or_resale[:1].transaction_type or False
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
    meli_stock_return_state = fields.Selection([
        ('partial', 'Partial'),
        ('done', 'Done'),
        ('settled', 'Settled'),
    ], string='Physical Return Status', copy=False,
        help="Tracks the PHYSICAL side of a non-Full order's total "
             "cancellation/return (2026-09-24 user request, Phase 2) — "
             "separate from and later than the automated stock-to-"
             "transit step (which only moves the order's stock to the "
             "shared 'Devoluciones en tránsito ML' location, never all "
             "the way back into real, sellable stock). Empty/unset for "
             "every order this doesn't apply to; only ever set by the "
             "manual quarantine-return wizard. 'Partial': some, but not "
             "all, of this order's stock was moved from transit into "
             "the warehouse's own quarantine location — the wizard can "
             "still be run again for whatever's left. 'Done': every "
             "unit was moved into quarantine — the order drops out of "
             "the pending-review filter and the wizard can no longer be "
             "run for it. 'Settled': some or all of this order's stock "
             "will never come back (written off) — a deliberate human "
             "decision via the wizard's own 'Settle' button, only "
             "available once this is already 'Partial'.",
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

    meli_order_detail_url = fields.Char(
        string='Mercado Libre Order Link', compute='_compute_meli_order_detail_url',
        help="Direct link to this order's own detail page on Mercado "
             "Libre's seller portal (2026-09-24, user-confirmed URL "
             "pattern, real example: https://vendedores.mercadolibre."
             "com.mx/ventas/2000014993970687/detalle). Same pack_id-"
             "first convention as meli_portal_url. MLM-only, like the "
             "rest of this module.",
    )

    @api.depends('reference', 'meli_order_id')
    def _compute_meli_order_detail_url(self):
        for order in self:
            # Fix 2026-09-24 (user-directed): sourced from order.reference
            # (falling back to meli_order_id) rather than meli_pack_id —
            # reference already carries the right id (pack id when this
            # order is part of one, the plain order id otherwise; see
            # _meli_create_from_order_data's own 'reference': customer_ref
            # assignment) for BOTH connector-created and Ventiapp-adopted
            # orders alike.
            portal_id = order.reference or order.meli_order_id
            order.meli_order_detail_url = (
                f'https://vendedores.mercadolibre.com.mx/ventas/{portal_id}/detalle'
                if portal_id else False
            )

    @api.model
    def _meli_import_order(self, company_id, order_id):
        """Entry point for queue_job — in this invoicing-only build,
        reached exclusively from meli.invoice.document._meli_upsert's
        Trigger B (an invoice/credit-note document arrived with no
        resolvable sale order at all). Idempotent: safe to call more
        than once for the same order — including its own delayed
        self-retry below, when this order is too recent to create yet.

        Fix 2026-09-18 (real, critical production bug — order S970525/
        pack 2000014889797151): this whole "invoicing-only" build has NO
        active order-discovery mechanism beyond a document's own
        meli_order_id (see controllers/main.py: the 'orders_v2' sale
        injector is deliberately not wired up here). Confirmed in
        production that Mercado Libre's own credit-note/factura
        metadata for a multi-item pack can report ONLY one sibling's
        order_id — even when its XML total covers the WHOLE pack — so
        the OTHER sibling's order_id never appears anywhere this
        connector was already looking, and its own product line never
        gets added at all (silently: no error, just a missing line and
        a permanent meli_amount_mismatch). Every call here now also
        checks the pack itself (GET /packs/$PACK_ID, confirmed against
        the live API to return every individual order in the pack) and
        imports any sibling order_id Odoo doesn't know about yet —
        regardless of whether THIS call found an existing order or
        needed to create one.
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
        pack_id = str(order_data.get('pack_id') or '') or False

        if existing:
            existing._meli_flag_status_change(order_data)
            existing._meli_retry_unmapped_lines(order_data)
            if pack_id:
                self._meli_ensure_all_pack_siblings_imported(config, pack_id, order_id)
            return existing

        # Fix 2026-09-14 (user request): give Ventiapp its own chance to
        # inject this order first, for a genuinely recent one — see
        # MELI_ORDER_RECOVERY_GRACE_MINUTES. Only delays when there's
        # nothing to adopt YET; an order Ventiapp already created (a real
        # adoption match) proceeds immediately below, same as always —
        # _meli_create_from_order_data adopts it there instead of
        # creating a duplicate. Same (reference, meli_sync_source=False,
        # state!=cancel) domain that method's own adoption match uses,
        # checked here first purely to decide whether to wait at all.
        adoption_ref = pack_id or order_id
        already_adoptable = bool(self.sudo().search_count([
            ('reference', '=', adoption_ref),
            ('meli_sync_source', '=', False),
            ('state', '!=', 'cancel'),
            '!', '&',
            ('create_uid', '=', MELI_ADOPTION_EXCLUDED_CREATE_UID),
            ('partner_id', '=', MELI_ADOPTION_EXCLUDED_PARTNER_ID),
        ]))
        if not already_adoptable:
            remaining_seconds = self._meli_order_recovery_delay_seconds(order_data)
            if remaining_seconds:
                self.with_delay(
                    priority=8, channel='root.meli_sales', max_retries=8,
                    identity_key=f"meli_recover_order_{order_id}",
                    eta=remaining_seconds,
                )._meli_import_order(company_id, order_id)
                return self.browse()

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
            self.env['meli.invoice.document']._meli_relink_orphaned_documents(
                order_id, pack_id,
            )
        if pack_id:
            self._meli_ensure_all_pack_siblings_imported(config, pack_id, order_id)
        return order

    def _meli_ensure_all_pack_siblings_imported(self, config, pack_id, known_order_id):
        """Fix 2026-09-18 (see _meli_import_order's own docstring for the
        real production bug this closes): fetches the pack's own,
        authoritative order list straight from Mercado Libre (GET
        /packs/$PACK_ID — confirmed in practice to return every
        individual order that belongs to it, each with its own 'id')
        and enqueues _meli_import_order for any sibling order_id this
        connector doesn't know about yet — neither as a sale.order's own
        meli_order_id (the pack's first-seen sibling) nor as a
        sale.order.line's own meli_order_id (a later sibling already
        added via _meli_add_pack_sibling_lines).

        Deliberately enqueues each missing sibling as its OWN delayed
        job (same identity_key convention as the grace-period recovery
        self-retry a few lines up in _meli_import_order, so the two
        naturally de-duplicate against each other) rather than importing
        it inline here — this keeps each sibling's own retry/failure
        isolated, exactly like every other recovery path in this file.

        Never raises: a failure to reach /packs/$PACK_ID must not break
        the ordinary single-order import this is always called
        alongside — logged and swallowed instead, same convention as
        every other best-effort side call in this module.
        """
        try:
            pack_data = config._api_get(f'/packs/{pack_id}')
        except Exception:
            _logger.exception(
                "Mercado Libre pack %s: could not fetch the pack's own "
                "order list — skipping the missing-sibling check for "
                "this call (will be retried the next time any order in "
                "this pack is imported/updated).",
                pack_id,
            )
            return
        sibling_ids = {
            str(pack_order.get('id')) for pack_order in pack_data.get('orders') or []
            if pack_order.get('id')
        } - {known_order_id}
        if not sibling_ids:
            return
        known_ids = set(self.sudo().search([
            ('meli_order_id', 'in', list(sibling_ids)),
        ]).mapped('meli_order_id'))
        known_ids |= set(self.env['sale.order.line'].sudo().search([
            ('meli_order_id', 'in', list(sibling_ids)),
        ]).mapped('meli_order_id'))
        for missing_order_id in sorted(sibling_ids - known_ids):
            # priority=0 (was 8, 2026-09-24 user-directed): this
            # connector is the only real consumer of this queue —
            # creating a sale is never lower priority than anything
            # else in it.
            self.with_delay(
                priority=0, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_recover_order_{missing_order_id}",
                description=(
                    f"Import missing pack sibling order {missing_order_id} "
                    f"(pack {pack_id})"
                ),
            )._meli_import_order(config.company_id.id, missing_order_id)

    def _meli_repair_pack_siblings_now(self, company_id):
        """One order's own share of action_meli_repair_all_historical_
        packs (2026-09-18 user request) — reuses _meli_ensure_all_pack_
        siblings_imported exactly, the same check-and-fix logic every
        automated import already runs. A genuine no-op (no API call
        even reached, let alone any job enqueued) whenever this order's
        own pack is already complete — safe to run indiscriminately
        over every historical pack order, not just ones already
        flagged with a mismatch.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config or not self.meli_pack_id:
            return
        self._meli_ensure_all_pack_siblings_imported(
            config, self.meli_pack_id, self.meli_order_id,
        )

    def _meli_repair_missing_shipping_now(self, company_id):
        """One order's own share of action_meli_repair_missing_shipping_
        lines (2026-09-19/20 user request) — closes the gap
        _meli_fetch_buyer_shipping_surcharge's own docstring explains
        for an order that was already imported before this fix
        existed. A genuine no-op whenever this order already has its
        own shipping-surcharge line, has no meli_order_id (not a
        Mercado Libre sale), or Mercado Libre's own shipment for it
        turns out to be 'custom' (already covered by the OTHER,
        pre-existing shipping-cost fetch — never double-charge).

        Fix 2026-09-22 (user request — folded into this SAME action
        rather than a new one): also backfills meli_shipping_id/meli_
        shipping_scheme for an order that predates those two fields
        (real case: order 2000018251129258/S952657) — both are only
        ever set once, at original creation time
        (_meli_create_from_order_data), with no repair of their own
        until now. Runs BEFORE the shipping-surcharge-line early
        returns below, so an order that already has its shipping-va
        line (or genuinely has no buyer surcharge to add) still gets
        this backfill — the two are independent gaps.
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config or not self.meli_order_id or not config.shipping_item_id:
            return
        if not self.meli_shipping_id or not self.meli_shipping_scheme:
            backfill_order_data = config._api_get(f'/orders/{self.meli_order_id}')
            backfill_shipping_id = (backfill_order_data.get('shipping') or {}).get('id')
            if backfill_shipping_id:
                backfill_shipments = self._meli_fetch_shipment_records(
                    config, self.meli_order_id, backfill_order_data,
                )
                backfill_logistic_type = self._meli_fetch_logistic_type(
                    config, self.meli_order_id, backfill_order_data,
                    shipments=backfill_shipments,
                )
                backfill_mode = next(
                    (s.get('mode') for s in backfill_shipments or [] if s.get('type') == 'forward'),
                    None,
                )
                backfill_vals = {}
                if not self.meli_shipping_id:
                    backfill_vals['meli_shipping_id'] = str(backfill_shipping_id)
                if not self.meli_shipping_scheme:
                    backfill_vals['meli_shipping_scheme'] = (
                        'full' if backfill_logistic_type == 'fulfillment'
                        else 'custom' if backfill_mode == 'custom'
                        else 'traditional' if backfill_mode == 'me2'
                        else False
                    )
                if backfill_vals:
                    self.write(backfill_vals)
        if self.order_line.filtered(lambda l: l.product_id == config.shipping_item_id):
            return
        order_data = config._api_get(f'/orders/{self.meli_order_id}')
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return
        shipments = self._meli_fetch_shipment_records(config, self.meli_order_id, order_data)
        buyer_shipping_cost = self._meli_fetch_buyer_shipping_surcharge(
            config, self.meli_order_id, order_data, shipments=shipments,
        )
        if not buyer_shipping_cost:
            return
        shipping_price_unit = self._meli_price_unit_untaxed(
            config.shipping_item_id, buyer_shipping_cost,
        )
        # Same unlock-before/relock-after pattern _meli_add_pack_sibling_
        # lines already uses to add a line to an order that's typically
        # already confirmed (and, for most companies' own settings,
        # already locked) — see that method's own comment for why a
        # plain field write, not action_unlock()/action_lock().
        was_locked = self.locked
        if was_locked:
            self.locked = False
        self.write({'order_line': [(0, 0, {
            'product_id': config.shipping_item_id.id,
            'product_uom_qty': 1,
            'price_unit': shipping_price_unit,
        })]})
        # Fix 2026-09-22 (real production bug, order S969001): this
        # company's own xe_pacific/models/sale_order_line.py overrides
        # _compute_qty_delivered to piggyback a SHIPPING-VA line's own
        # qty_delivered onto whichever OTHER (real, stockable) line on
        # the same order just became delivered — but that override is
        # itself only ever triggered by ITS OWN @api.depends
        # ('move_ids.state', ...), which fires when the main product's
        # own delivery state changes, never just because a brand new
        # line got added to an order that was already delivered long
        # ago (nothing about adding this line touches move_ids at all).
        # Left alone, a shipping-va line added here this way — after
        # the fact, on an order already fully delivered — permanently
        # reads qty_delivered=0, so _create_invoices() (delivery-based
        # invoicing policy) never invoices it: the order's own total
        # looks corrected, but the actual posted invoice never
        # includes this line, and it never shows as delivered/invoiced
        # either. Set directly here, mirroring exactly what that
        # override does for a normal, same-time delivery — but only
        # when this order's other real line(s) are already delivered;
        # never invented for an order that genuinely hasn't shipped
        # yet, where the normal flow will handle it in due course.
        new_shipping_line = self.order_line.filtered(
            lambda l: l.product_id == config.shipping_item_id
        )
        if new_shipping_line and any(
            self.order_line.filtered(lambda l: l.product_id != config.shipping_item_id)
            .mapped('qty_delivered')
        ):
            new_shipping_line.qty_delivered = new_shipping_line.product_uom_qty
        if was_locked:
            self.locked = True
        self.message_post(body=_(
            "Added the missing Mercado Envíos shipping line "
            "($%(cost)s, the buyer's own share reported by Mercado "
            "Libre) — this sale's total was previously short by "
            "exactly that amount compared to the real invoice. "
            "Reconciliation was re-triggered automatically."
        ) % {'cost': '%.2f' % buyer_shipping_cost})
        self._meli_reconcile_invoicing()

    def action_meli_repair_missing_shipping_lines(self):
        """Server action (2026-09-19/20 user request), deliberately NOT
        exposed as a button — same convention as
        action_meli_repair_all_historical_packs (wire it up yourself as
        an ir.actions.server: `action = model.action_meli_repair_
        missing_shipping_lines()`).

        Finds every Mercado Libre sale already flagged with an amount
        mismatch (meli.invoice.document.meli_amount_mismatch — the same
        symptom this whole gap causes) — in the current selection if
        any, otherwise model-wide — and enqueues one cheap, idempotent
        job per order via _meli_repair_missing_shipping_now. Scoped to
        already-mismatched orders rather than every single Mercado
        Libre sale: re-fetching /shipments/{id}/costs for every order
        ever imported would be a lot of unnecessary API traffic for
        orders that were never affected by this gap in the first place
        (custom-shipping orders, orders with no shipping charge to the
        buyer at all, etc.).
        """
        if self:
            orders = self
        else:
            orders = self.env['meli.invoice.document'].sudo().search([
                ('meli_amount_mismatch', '=', True),
                ('sale_order_id', '!=', False),
            ]).mapped('sale_order_id')
        queued = 0
        for order in orders:
            order.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_shipping_historical_repair_{order.id}",
                description=f"Historical shipping-line repair check for {order.name}",
            )._meli_repair_missing_shipping_now(order.company_id.id)
            queued += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(queued)s venta(s) puesta(s) en cola para revisión "
                    "de envío faltante — se corrigen solas en segundo "
                    "plano, sin afectar las que ya están completas."
                ) % {'queued': queued},
                'type': 'success',
            },
        }

    def _meli_repair_wrong_shipping_line_now(self, company_id):
        """The opposite cleanup from _meli_repair_missing_shipping_now
        above: removes a buyer-shipping-surcharge line from a genuine
        resale/1P order (real case order 2000018568677372/pack
        2000015136237497) — Mercado Libre's own shipping charge to the
        buyer on a resale order is its own resale markup, never money
        owed to the seller, so that line never belongs on the sale.

        Fix 2026-09-22: called automatically from the very top of
        _meli_reconcile_invoicing on every single call now (see that
        method's own comment) — not just on demand via
        action_meli_repair_wrong_shipping_lines. Since 2026-09-22,
        _meli_fetch_buyer_shipping_surcharge no longer tries to guess
        "is this resale" upfront (a real production regression proved
        the 'catalog' tag it used to check is not a reliable signal —
        see that method's own docstring) — every order gets the buyer's
        real shipping charge added unconditionally instead, and THIS
        method is what takes it back out again the moment
        meli_transaction_type is actually confirmed 'resale'. Still kept
        as its own action too, for any order that slipped through before
        this was wired into every reconcile call.

        A genuine no-op whenever this order isn't 'resale'
        (meli_transaction_type) or doesn't actually have a
        shipping-surcharge line to remove. Cancels the existing
        invoice first — breaking payment reconciliation if it has to,
        same mechanism used elsewhere in this file — so the line can
        be removed cleanly; the normal reconciliation flow right after
        creates a fresh, correct invoice matching the real document
        (now with nothing left to disagree about).
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config or not config.shipping_item_id:
            return
        if self.meli_transaction_type != 'resale':
            return
        wrong_line = self.order_line.filtered(
            lambda l: l.product_id == config.shipping_item_id
        )
        if not wrong_line:
            return
        current_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )[:1]
        if current_invoice:
            self._meli_cancel_account_move_breaking_reconciliation(current_invoice)
        removed_amount = sum(wrong_line.mapped('price_total'))
        was_locked = self.locked
        if was_locked:
            self.locked = False
        wrong_line.unlink()
        if was_locked:
            self.locked = True
        self.message_post(body=_(
            "Removed a shipping line ($%(amount)s) that was wrongly "
            "added to this catalog/resale order before the 2026-09-21 "
            "fix — Mercado Libre's own shipping charge to the buyer on "
            "a resale order is its own resale markup, never money owed "
            "to the seller. Reconciliation was re-triggered "
            "automatically."
        ) % {'amount': '%.2f' % removed_amount})
        self._meli_reconcile_invoicing()

    def action_meli_repair_wrong_shipping_lines(self):
        """Server action (2026-09-21 user request), deliberately NOT
        exposed as a button — same convention as every other repair
        action in this file (wire it up yourself as an
        ir.actions.server: `action = model.action_meli_repair_wrong_
        shipping_lines()`).

        Finds every Mercado Libre sale already known to be 'resale'
        (meli_transaction_type — set once its own invoice document
        arrives) — in the current selection if any, otherwise
        model-wide — and enqueues one cheap, idempotent job per order
        via _meli_repair_wrong_shipping_line_now. Scoped to resale
        orders rather than every Mercado Libre sale: an ordinary sale
        never had this line wrongly added in the first place, so
        there's nothing there to check.
        """
        if self:
            orders = self
        else:
            orders = self.sudo().search([('meli_transaction_type', '=', 'resale')])
        queued = 0
        for order in orders:
            order.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_wrong_shipping_repair_{order.id}",
                description=f"Wrong shipping line repair check for {order.name}",
            )._meli_repair_wrong_shipping_line_now(order.company_id.id)
            queued += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(queued)s venta(s) de reventa puesta(s) en cola "
                    "para revisión de envío indebido — se corrigen "
                    "solas en segundo plano, sin afectar las que ya "
                    "están correctas."
                ) % {'queued': queued},
                'type': 'success',
            },
        }

    def _meli_repair_missing_coupon_discount_now(self, company_id):
        """One order's own share of action_meli_repair_missing_coupon_
        discount (2026-09-21 user request) — closes the gap for a sale
        imported before meli_discount_amount/meli_discount_ml_funded_
        amount existed: re-fetches this order's real, current
        order_items[].discounts from the live API and backfills both
        informational fields on every matching line.

        Fix 2026-09-22 (user-directed correction): price_unit itself IS
        corrected here too, when it's wrong — this was thought to be
        unnecessary at first (a CONNECTOR-built line's own price_unit
        already comes straight from order_items[].unit_price, Mercado
        Libre's own already-net-of-coupon buyer price, matching the
        real CFDI — see _meli_build_order_lines), but that guarantee
        only ever held for a line THIS connector itself built. A sale
        VentiApp created and this connector only later adopted (see
        sale.order.meli_adopted's own help text: "adoption never
        touches an order's own commercial details — lines, pricing...
        left untouched") can carry whatever price VentiApp's own,
        separate logic used, which may never have accounted for a
        coupon at all. So: re-fetch the real unit_price for each line,
        and if it doesn't match what's actually on the sale, fix it —
        then re-trigger reconciliation so any invoice/credit note
        already built from the old, wrong amount gets cancelled and
        rebuilt to match, exactly like the two shipping-line repairs
        above.

        Uses _meli_sibling_lines' own robust matching (falls back to
        every one of self.order_line when no line carries its own
        meli_order_id at all — the exact shape of a VentiApp-adopted,
        non-pack order) instead of a plain filter on
        line.meli_order_id, which would find nothing at all for such
        an order — a CONNECTOR-built line always carries its own
        meli_order_id (see _meli_build_order_lines), but an adopted
        VentiApp line never does.

        Fix 2026-09-22 (real production case, order S968823/pack
        2000015052767879 — deliberately folded into this SAME action
        rather than a new one, "no quiero 10 mil acciones de
        servidor"): order_items[].unit_price is the CONSUMER catalog
        price even for a 'resale'/1P order — Mercado Libre pays XE a
        separate, lower WHOLESALE price for these that never appears
        anywhere in the order resource itself, only in the real,
        already-authorized factura's own XML (confirmed: XML
        ValorUnitario $80.8275 vs. this order's own line at $112.93 —
        exactly the $297.91 total mismatch). Using the order API's own
        unit_price for a resale line would just reinforce that same
        wrong (consumer) price. Once this order is already known to be
        resale (meli_transaction_type — set only once its own factura
        document exists), the real per-unit price is read from that
        document's own XML instead — matched by product name, same
        technique _meli_build_partial_credit_note already uses — and
        used AS-IS (CFDI amounts are already pre-tax; never divided by
        MELI_MX_IVA_RATE the way the order API's own consumer price
        needs to be).
        """
        self.ensure_one()
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config or not self.meli_order_id:
            return
        order_data = config._api_get(f'/orders/{self.meli_order_id}')
        order_items = order_data.get('order_items') or []
        lines = self._meli_sibling_lines(self.meli_order_id)
        price_changed = False

        resale_xml_price_by_product_id = {}
        if self.meli_transaction_type == 'resale':
            from .meli_invoice_document import MELI_INVOICE_DEAD_STATUSES
            factura = self.meli_invoice_document_ids.filtered(
                lambda d: d.document_type == 'factura' and d.xml_file
                and d.status not in MELI_INVOICE_DEAD_STATUSES
            ).sorted(key=lambda d: (d.create_date, d.id))[-1:]
            if factura:
                xml_bytes = base64.b64decode(factura.xml_file)
                concepts = factura._meli_parse_concepts_from_xml(xml_bytes)
                product_lines = self.order_line.filtered(
                    lambda l: l.product_id and not l.display_type
                )

                def _normalize(text):
                    return ' '.join((text or '').split()).casefold()

                for concept in concepts:
                    matches = product_lines.filtered(
                        lambda l: _normalize(l.product_id.name) == _normalize(concept['descripcion'])
                    )
                    if len(matches) == 1 and concept['cantidad']:
                        resale_xml_price_by_product_id[matches.product_id.id] = (
                            concept['importe'] / concept['cantidad']
                        )

        for order_item in order_items:
            item = order_item.get('item') or {}
            sku = (item.get('seller_sku') or '').strip()
            product = self.env['meli.sku.mapping']._resolve_product_by_meli_sku(sku)
            if not product:
                continue
            line = lines.filtered(lambda l: l.product_id == product)[:1]
            if not line:
                continue
            unit_discount_full = 0.0
            unit_discount_seller = 0.0
            for discount in (order_item.get('discounts') or []):
                amounts = discount.get('amounts') or {}
                unit_discount_full += amounts.get('full') or 0.0
                unit_discount_seller += amounts.get('seller') or 0.0
            ml_funded_unit_amount = max(unit_discount_full - unit_discount_seller, 0.0)
            quantity = line.product_uom_qty or 1
            new_discount_amount = unit_discount_full * quantity
            new_ml_funded_amount = ml_funded_unit_amount * quantity
            line_vals = {}
            if abs(new_discount_amount - line.meli_discount_amount) > 0.01:
                line_vals['meli_discount_amount'] = new_discount_amount
            if abs(new_ml_funded_amount - line.meli_discount_ml_funded_amount) > 0.01:
                line_vals['meli_discount_ml_funded_amount'] = new_ml_funded_amount
            # Resale: the real per-unit price comes from the factura's
            # own XML (already pre-tax, used as-is) — see this method's
            # own docstring for why the order API's unit_price is the
            # wrong (consumer) source for a resale line.
            if product.id in resale_xml_price_by_product_id:
                correct_price_unit = resale_xml_price_by_product_id[product.id]
                if abs(correct_price_unit - line.price_unit) > 0.01:
                    line_vals['price_unit'] = correct_price_unit
                    price_changed = True
            else:
                # A genuinely missing unit_price (None, not 0.0 — see
                # _meli_build_order_lines' own null-price fallback) is a
                # rare edge case this repair deliberately leaves alone
                # rather than guessing; only ever corrects price_unit
                # when Mercado Libre's own API actually reports one.
                ml_unit_price = order_item.get('unit_price')
                if ml_unit_price is not None:
                    correct_price_unit = self._meli_price_unit_untaxed(product, ml_unit_price)
                    if abs(correct_price_unit - line.price_unit) > 0.01:
                        line_vals['price_unit'] = correct_price_unit
                        price_changed = True
            if line_vals:
                was_locked = self.locked
                if was_locked:
                    self.locked = False
                line.write(line_vals)
                if was_locked:
                    self.locked = True
        if price_changed:
            self.message_post(body=_(
                "Corrected the price of one or more lines to Mercado "
                "Libre's own real, coupon-adjusted unit price — this "
                "sale was originally created outside this connector "
                "(see meli_adopted) with a price that never accounted "
                "for a coupon applied on Mercado Libre's own side. "
                "Reconciliation was re-triggered automatically so any "
                "existing invoice/credit note matches the corrected "
                "amount."
            ))
            self._meli_reconcile_invoicing()

    def action_meli_repair_missing_coupon_discount(self):
        """Server action (2026-09-21 user request), deliberately NOT
        exposed as a button — same convention as every other repair
        action in this file (wire it up yourself as an
        ir.actions.server: `action = model.action_meli_repair_missing_
        coupon_discount()`).

        Every Mercado Libre sale (in the current selection if any,
        otherwise model-wide) gets one cheap, idempotent job via
        _meli_repair_missing_coupon_discount_now — deliberately NOT
        scoped to already-flagged/mismatched orders the way the
        shipping repair above is: a coupon-discount gap has no
        existing flag of its own to search by (unlike meli_amount_
        mismatch for the shipping case), so every historical order
        needs its own real check against the live API.
        """
        if self:
            orders = self
        else:
            orders = self.sudo().search([('meli_order_id', '!=', False)])
        queued = 0
        for order in orders:
            order.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_coupon_discount_repair_{order.id}",
                description=f"Coupon discount repair check for {order.name}",
            )._meli_repair_missing_coupon_discount_now(order.company_id.id)
            queued += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(queued)s venta(s) puesta(s) en cola para revisión "
                    "de cupón/descuento — se corrigen solas en segundo "
                    "plano, sin afectar las que ya están completas."
                ) % {'queued': queued},
                'type': 'success',
            },
        }

    def action_meli_repair_all_historical_packs(self):
        """Server action (2026-09-18 user request), deliberately NOT
        exposed as a button — meant to be wired up as an
        ir.actions.server the user creates themselves (Settings >
        Technical > Server Actions, bound to this model, "Execute
        Python Code": `action = model.action_meli_repair_all_historical_
        packs()`), so it runs model-wide (no selection needed) and can
        be toggled on/off independently of a code deploy.

        Finds every pack order (meli_pack_id set) and enqueues one
        cheap, idempotent job per order via _meli_repair_pack_siblings_
        now — never calls the Mercado Libre API inline here, so this
        request itself returns immediately regardless of how many
        thousands of pack orders exist. queue_job's own worker
        concurrency for channel 'root.meli_sales' paces the real API
        calls naturally; no manual rate-limiting needed here. Safe to
        run over EVERY pack order, not just ones already flagged with
        a mismatch — an already-complete pack costs one API call and
        nothing else (see _meli_ensure_all_pack_siblings_imported's own
        docstring).
        """
        orders = self if self else self.sudo().search([('meli_pack_id', '!=', False)])
        queued = 0
        for order in orders:
            order.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_pack_historical_repair_{order.id}",
                description=f"Historical pack repair check for {order.name}",
            )._meli_repair_pack_siblings_now(order.company_id.id)
            queued += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(queued)s venta(s) de pack puesta(s) en cola para "
                    "revisión — se corrigen solas en segundo plano, sin "
                    "afectar las que ya están completas."
                ) % {'queued': queued},
                'type': 'success',
            },
        }

    def _meli_infer_cancelled_from_credit_note(self):
        """Called whenever a credit-note/devolution document resolves to
        this non-Full order (2026-09-24 user request, Phase 1 of the
        Monterrey XE2 total-cancellation project) — a live 'cancelled'
        notification is the normal way meli_last_status gets set, but
        Mercado Libre issuing a real devolución/nota de crédito against
        this order IS itself proof the order was cancelled, whether or
        not that notification ever arrived (or ever will: every order
        adopted from VentiApp before this connector tracked status at
        all has no meli_last_status of its own, no matter how it later
        gets cancelled on Mercado Libre's side). Keeping
        meli_last_status as the single source of truth this way — updated
        here, never a separate/parallel check duplicated elsewhere —
        means every other piece of this project (the pending-review
        filter, the eventual automated stock-to-transit step) can simply
        trust this one field instead of re-deriving "is this genuinely
        cancelled" its own way. A one-off Server Action reusing this
        same method is how the pre-existing backlog of already-arrived
        documents gets caught up once, historically; nothing here is
        specific to that one-time run.

        Deliberately a no-op for a Full order: a live 'cancelled'
        notification always arrives for those (Mercado Libre's own
        fulfillment flow depends on it), so meli_last_status is already
        reliable there — this exists only to cover non-Full's real gap.
        """
        self.ensure_one()
        if self.meli_last_status == 'cancelled':
            return
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)
        is_full = bool(
            config and config.warehouse_fulfillment_id
            and self.warehouse_id == config.warehouse_fulfillment_id
        )
        if is_full:
            return
        self.meli_last_status = 'cancelled'

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
        credit_note_documents = self.env['meli.invoice.document'].sudo().search([
            ('sale_order_id', '=', self.id),
            ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
        ])
        # Fix 2026-09-16 (real production bug, order S841005/pack
        # 2000014914865611): is_applied only tracks whether THIS
        # sibling's credit note was ever related (meli.invoice.document.
        # move_ids non-empty) — but _meli_apply_partial_cancellation's
        # own docstring is explicit that relating the credit note and
        # physically returning the stock are two separate halves of the
        # same job. Once a sibling's credit note gets related — whether
        # by this same button on an earlier click, or by
        # _meli_reconcile_invoicing's own pack branch (which ONLY
        # relates the credit note, on purpose — see its own comment on
        # why it never returns stock automatically there) —
        # is_applied flips True and this button's old is_applied=False
        # search could never find that sibling again, even though its
        # stock was never returned. Confirmed in production: exactly
        # this state (credit note related, delivered stock still sitting
        # on the books) for a sibling whose automatic first attempt at
        # _meli_process_partial_cancellation had already failed and
        # degraded to manual review once. Re-checking delivered vs.
        # returned quantity from the real stock moves (never trusting a
        # boolean flag that can't tell the two halves apart) closes that
        # gap without touching the already-applied credit note itself —
        # _meli_apply_partial_cancellation's own credit-note step is
        # idempotent (see _meli_relate_partial_cancellation_credit_note's
        # own docstring), so re-running it here for an
        # already-is_applied sibling is always safe.
        stuck_sibling_ids = set()
        for sibling_id in set(credit_note_documents.mapped('meli_order_id')) - {False}:
            sibling_documents = credit_note_documents.filtered(
                lambda d, sibling_id=sibling_id: d.meli_order_id == sibling_id
            )
            if not all(sibling_documents.mapped('is_applied')):
                stuck_sibling_ids.add(sibling_id)
                continue
            lines = self._meli_sibling_lines(sibling_id)
            if not lines:
                continue
            delivered_qty, returned_qty = self._meli_sibling_delivered_and_returned_qty(lines)
            if delivered_qty > returned_qty:
                stuck_sibling_ids.add(sibling_id)
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

    def action_meli_refresh_status_and_reconcile(self):
        """Manual button/action (2026-09-22 user request): refreshes
        this order's own cached live Mercado Libre status
        (meli_last_status) directly from the API — it can be stale,
        especially on an older order that hasn't had a status-change
        webhook land in a while — then re-runs the normal, idempotent
        _meli_reconcile_invoicing(), which decides what to do (build a
        confirmed partial-refund credit note, apply a full
        cancellation, etc.) from that SAME live status it re-checks
        internally on its own.

        Refreshing meli_last_status here first is purely for
        visibility: a human looking at this order's own record
        afterward sees exactly what Mercado Libre says right now,
        before deciding whether anything still needs to be forced
        manually (e.g. via _meli_build_partial_credit_note directly,
        for an order whose live status has since moved past
        'partially_refunded' and can no longer be picked up by the
        ordinary reconcile path at all).
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
        live_order_data = config._api_get(f'/orders/{self.meli_order_id}')
        self.meli_last_status = (live_order_data or {}).get('status')
        self._meli_reconcile_invoicing()

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
            self._meli_notify_price_fallbacks(price_debug)
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

        Fix 2026-09-18 (real production bug, confirmed by direct
        reproduction against this exact database): core Odoo's own
        sale.order.line._action_launch_stock_rule() silently no-ops —
        no exception, nothing logged, nothing created — for every line
        whose order.locked is True (see that method's own `if
        line.order_id.locked: continue` guard). Every order confirmed
        here ends up locked almost immediately in practice (this
        company's sale.group_auto_done_setting auto-locks on
        confirmation for most users — see the identical was_locked
        pattern already used by _meli_add_pack_sibling_lines, itself
        fixed for the same reason on 2026-09-16), so any order whose
        picking is missing/cancelled by the time this method runs
        (e.g. some other automation cancelled it, or it never got
        created at all) previously had this call do NOTHING — not even
        the "needs manual review" chatter message below, since no
        exception was ever raised to trigger it. That's a silent,
        invisible failure worse than the message this method already
        posts for a genuine error. Same brief unlock/relock this file
        already established elsewhere (never action_unlock()/
        action_lock() — see _meli_add_pack_sibling_lines's own
        docstring for why a plain field write is deliberate here).
        """
        self.ensure_one()
        if not self.picking_ids.filtered(lambda p: p.state != 'cancel'):
            was_locked = self.locked
            if was_locked:
                self.locked = False
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
                if was_locked:
                    self.locked = True
                return
            if was_locked:
                self.locked = True
            if not self.picking_ids.filtered(lambda p: p.state != 'cancel'):
                # Belt-and-suspenders: covers any OTHER silent-no-op
                # path core Odoo might have (present or future) beyond
                # the locked-order one this fix already closes — never
                # leave a confirmed sale with no delivery and no alert
                # at all.
                self._meli_post_with_mention(_(
                    "This sale is confirmed but its delivery transfer "
                    "could not be generated automatically. Please "
                    "review and create it manually."
                ))
                return
        if is_fulfillment:
            self._meli_auto_validate_full_pickings()

    @api.model
    def _cron_retry_missing_deliveries(self):
        """30-minute safety-net cron (xe_meli_connector/data/ir_cron.xml,
        2026-09-22 user request): _meli_ensure_delivery's own error
        handling degrades to "needs manual review" with no automatic
        retry of its own — unlike invoicing (meli.invoice.document.
        _cron_retry_unapplied_documents), a confirmed Mercado Libre sale
        that fails to get its delivery transfer just sits broken
        forever unless a human notices the chatter message and creates
        it by hand (real case: order S974690, 2026-09-20).

        Finds every confirmed ('sale') Mercado Libre sale with no live
        outgoing picking and retries _meli_ensure_delivery on it here,
        in the background — a transient cause (a momentary lock, a
        route not yet loaded, the same kind of Postgres serialization
        conflict _meli_create_from_order_data's own action_confirm()
        call already treats as retryable) then corrects itself within
        30 minutes, with nobody needing to remember to look. Each order
        is retried in its own try/except: one order raising an
        unexpected error must not block every other stuck order in this
        run.
        """
        stuck_orders = self.sudo().search([
            ('state', '=', 'sale'),
            ('meli_sync_source', '!=', False),
        ]).filtered(
            lambda order: not order.picking_ids.filtered(lambda p: p.state != 'cancel')
        )
        for order in stuck_orders:
            try:
                order._meli_ensure_delivery('MLF' in (order.origin or ''))
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: automatic retry of the "
                    "missing delivery transfer failed — needs manual "
                    "review.", order.client_order_ref,
                )

    def _meli_force_deliver_cancelled_sibling_line(self, sibling_order_id):
        """Fix 2026-09-19 (user-directed, real production case): a pack
        sibling discovered so late that its own line only gets added
        AFTER this whole Full pack was already cancelled has no way to
        ever get delivered through the ordinary path — core Odoo's own
        sale.order.line._action_launch_stock_rule() unconditionally
        skips every line whose order.state isn't 'sale' (the same core
        guard that also silently skips a merely-locked order — see
        _meli_ensure_delivery's own Fix 2026-09-18 docstring for that
        sibling case). Without this, the line sits forever with no
        delivered stock, which then also means _meli_apply_partial_
        cancellation has nothing to return and no invoiced line to
        credit-note against — the whole downstream repair (stock
        return, invoice correction via refacturación, credit note)
        never gets a chance to run at all.

        Confirmed by the user: a Full pack ships as ONE physical
        parcel — if the rest of the pack was genuinely delivered, this
        sibling's own unit left the warehouse right along with it,
        whether or not Odoo ever recorded that. Only ever run when
        that's actually true: skipped entirely when this order has no
        'done' outgoing picking of its own yet (see the caller — a
        pack that was cancelled BEFORE it ever physically shipped has
        nothing to retroactively deliver, and forcing a fake delivery
        for it would fabricate history that never happened).

        Briefly flips this order's own state to 'sale' (mail tracking
        disabled — this is bookkeeping, not a real status change a
        human should see logged) purely so Odoo's own real stock-rule
        mechanism runs for this ONE new line. Deliberately never calls
        action_confirm()/action_cancel() themselves — only the bare
        state field, restored immediately after — so none of those
        buttons' OTHER side effects (chatter, re-reconciling invoicing,
        etc.) fire here. Isolated in its own savepoint: any failure
        leaves the order exactly as it was (state back to 'cancel',
        line still added) — the caller's own downstream reconciliation
        degrades to its ordinary "nothing delivered yet" no-op, same
        as if this method didn't exist at all.
        """
        self.ensure_one()
        if self.state != 'cancel':
            return
        if not self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_id.code == 'outgoing'
        ):
            return
        lines = self.order_line.filtered(lambda l: l.meli_order_id == sibling_order_id)
        if not lines:
            return
        try:
            with self.env.cr.savepoint():
                self.with_context(tracking_disable=True).write({'state': 'sale'})
                lines._action_launch_stock_rule()
                self._meli_auto_validate_full_pickings()
        except Exception:
            _logger.exception(
                "Mercado Libre order %s: could not retroactively "
                "deliver sibling %s's own line on this already-"
                "cancelled pack — its stock return/credit note will "
                "stay pending manual review.",
                self.client_order_ref, sibling_order_id,
            )
        finally:
            self.with_context(tracking_disable=True).write({'state': 'cancel'})

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

    def _meli_return_full_pickings_to_transit(self):
        """Same by-code stock.return.picking mechanism as
        _meli_return_full_pickings, but always to the shared
        'Devoluciones en tránsito ML' location (2026-09-24 user
        request, Monterrey XE2 total-cancellation project, Phase 1)
        instead of the warehouse's own configured return location — a
        non-Full order's stock genuinely left XE's control via a real
        carrier; crediting it straight back into sellable stock the
        instant Mercado Libre reports a cancellation would be lying to
        inventory before the product has physically come back. Phase
        2's own manual quarantine-return wizard (not built yet) is
        what later confirms the product's real arrival and moves it
        the rest of the way, once a human looks at it.
        """
        self.ensure_one()
        location = self.env['stock.location'].sudo().search([
            ('name', '=', 'Devoluciones en tránsito ML'),
            ('company_id', 'in', [self.company_id.id, False]),
        ], limit=1)
        if not location:
            raise UserError(_(
                "The shared 'Devoluciones en tránsito ML' location is "
                "not configured — cannot return stock automatically."
            ))
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
                    "Automatic validation of the return transfer %s "
                    "needs manual confirmation (e.g. insufficient "
                    "stock) — cannot auto-cancel this order."
                ) % new_picking.name)
            new_pickings |= new_picking
        return new_pickings

    def _meli_return_sibling_pickings_to_transit(self, lines):
        """Same by-code stock.return.picking mechanism and same
        transit destination as _meli_return_full_pickings_to_transit,
        but scoped to ONE pack sibling's own line(s) only — same
        move-scoping technique _meli_apply_partial_cancellation already
        uses for its own (Full-only) stock return. 2026-09-25 user
        request: a non-Full pack sibling reported genuinely CANCELLED
        (not just a partial refund) gets its own share of Phase 1's
        total-cancellation policy — only THIS sibling's delivered
        stock goes to transit; every other, still-legitimate sibling
        in the pack is left completely untouched.
        """
        self.ensure_one()
        location = self.env['stock.location'].sudo().search([
            ('name', '=', 'Devoluciones en tránsito ML'),
            ('company_id', 'in', [self.company_id.id, False]),
        ], limit=1)
        if not location:
            raise UserError(_(
                "The shared 'Devoluciones en tránsito ML' location is "
                "not configured — cannot return stock automatically."
            ))
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
            return_wizard = self.env['stock.return.picking'].with_context(
                active_ids=picking.ids, active_id=picking.id, active_model='stock.picking',
            ).create({
                'location_id': location.id,
                'picking_id': picking.id,
                'product_return_moves': return_lines,
            })
            new_picking_id, __ = return_wizard._create_returns()
            new_picking = self.env['stock.picking'].browse(new_picking_id)
            result = new_picking.button_validate()
            if isinstance(result, dict):
                raise UserError(_(
                    "Automatic validation of the return transfer %s "
                    "needs manual confirmation (e.g. insufficient "
                    "stock) — cannot auto-process this sibling's "
                    "cancellation."
                ) % new_picking.name)
            new_pickings |= new_picking
        if new_pickings:
            self.message_post(body=_(
                "Individual order %(order_id)s's own stock was "
                "returned to the transit location 'Devoluciones en "
                "tránsito ML' via transfer(s) %(pickings)s — pending "
                "physical confirmation before it re-enters real stock."
            ) % {
                'order_id': lines.mapped('meli_order_id')[:1] or '?',
                'pickings': ', '.join(new_pickings.mapped('name')),
            })
        return new_pickings

    def _meli_process_non_full_total_cancellation(self):
        """Phase 1 of the Monterrey XE2 (non-Full) total-cancellation
        project (2026-09-24 user request) — the non-Full counterpart
        of _meli_process_full_cancellation, called from the same place
        (this order's credit-note step in _meli_reconcile_invoicing)
        but in the OPPOSITE order: the caller builds and posts the
        credit note BEFORE calling this method, never after. The user's
        own condition for cancelling ("una vez teniendo entregado 0 y
        facturado 0") can only both be true once the credit note
        already exists — Full's own pipeline runs stock+cancel first
        instead because neither condition matters for a Full order: the
        inventory never really left XE's own control.

        Returns stock to the shared transit location, never straight
        back into real, sellable stock — see
        _meli_return_full_pickings_to_transit's own docstring for why.
        Raises on any failure — the caller wraps this in a savepoint
        and falls back to manual review, same convention as
        _meli_process_full_cancellation.
        """
        self.ensure_one()
        self = self.with_context(meli_reconciling_invoicing=True)
        actions = []
        returns = self._meli_return_full_pickings_to_transit()
        if returns:
            actions.append(_(
                "Stock was returned to the transit location "
                "'Devoluciones en tránsito ML' via transfer(s) "
                "%(pickings)s — pending physical confirmation before "
                "it re-enters real stock."
            ) % {'pickings': ', '.join(returns.mapped('name'))})
        if self.locked:
            self.action_unlock()
        self.with_context(disable_cancel_warning=True).action_cancel()
        actions.append(_("The sale order was cancelled."))
        self.meli_auto_cancellation_processed = True
        return actions

    def action_meli_open_quarantine_return_wizard(self):
        """Manual button (2026-09-24 user request, Phase 2 of the
        Monterrey XE2 total-cancellation project) — opens the wizard
        that physically confirms how much of this order's stock, sitting
        in the shared 'Devoluciones en tránsito ML' location since Phase
        1 ran, has actually come back to the warehouse. Only meant for a
        Mercado Libre order genuinely already cancelled by Phase 1 —
        re-checked here, not just trusted from whatever list/filter the
        user clicked this from, since that view can lag behind a very
        recent change.
        """
        self.ensure_one()
        # Fix 2026-09-25 (real bug, this "invoicing-only" build):
        # meli_sync_source is only ever set by this connector's own
        # order-adoption/creation logic, which isn't wired up in every
        # deployment (see this module's own manifest description) — an
        # order whose invoicing this connector already handles can
        # still have meli_sync_source empty, VentiApp having created it
        # with no adoption step ever running. meli_last_status is a
        # more reliable "is this genuinely Mercado Libre" signal here:
        # nothing else in this module ever sets it.
        if not self.meli_last_status:
            raise UserError(_(
                "This is not a Mercado Libre order."
            ))
        if self.meli_last_status != 'cancelled':
            raise UserError(_(
                "This order's own Mercado Libre status is not "
                "'cancelled' — refresh and try again."
            ))
        # Fix 2026-09-25 (user-directed correction): meli_last_status
        # can already read 'cancelled' before the sale itself actually
        # is — that field is set the moment a credit-note document
        # arrives (_meli_infer_cancelled_from_credit_note), which can
        # happen before Phase 1 finishes applying the credit note and
        # cancelling the sale (e.g. no posted invoice yet to credit
        # against). Nothing to physically confirm until the sale is
        # genuinely cancelled.
        if self.state != 'cancel':
            raise UserError(_(
                "This sale hasn't been cancelled yet — nothing to "
                "confirm physically."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Confirm Physical Return"),
            'res_model': 'meli.quarantine.return.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_sale_order_id': self.id},
        }

    def _meli_quarantine_transit_pickings(self):
        """This order's own delivery-to-transit picking(s), created by
        _meli_return_full_pickings_to_transit (Phase 1) — the source of
        truth for what's left to move into quarantine (Phase 2), read
        straight from the real, already-validated transfers rather than
        any separate counter that could go stale.
        """
        self.ensure_one()
        transit_location = self.env['stock.location'].sudo().search([
            ('name', '=', 'Devoluciones en tránsito ML'),
            ('company_id', 'in', [self.company_id.id, False]),
        ], limit=1)
        if not transit_location:
            return self.env['stock.picking'], self.env['stock.move']
        pickings = self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.location_dest_id == transit_location
        )
        return pickings, pickings.move_ids.filtered(lambda m: m.state == 'done')

    def _meli_quarantine_remaining_by_product(self):
        """{product: remaining quantity} still sitting in transit for
        this order and not yet moved into quarantine — the SAME
        delivered/returned-quantity bookkeeping pattern used everywhere
        else in this module (see
        _meli_sibling_delivered_and_returned_qty), applied one level
        further: here 'delivered' means 'moved into transit' and
        'returned' means 'moved from transit into quarantine'.
        """
        self.ensure_one()
        __, transit_moves = self._meli_quarantine_transit_pickings()
        remaining = {}
        for move in transit_moves:
            already_moved_on = sum(
                move.returned_move_ids.filtered(
                    lambda m: m.state == 'done'
                ).mapped('quantity')
            )
            qty = move.quantity - already_moved_on
            if qty > 0:
                remaining[move.product_id] = remaining.get(move.product_id, 0.0) + qty
        return remaining

    def _meli_quarantine_move_stock(self, quantities_by_product, location_id):
        """The wizard's own Confirm action: moves whatever quantity the
        user entered per product from transit into the chosen quarantine
        location, by returning the specific transit move(s) that still
        have that much left — same by-code stock.return.picking pattern
        as every other stock movement in this module. Raises UserError
        (never a silent failure) if there isn't enough left to move for
        some product, or if Odoo can't auto-validate the transfer (e.g.
        wants a backorder/insufficient-stock confirmation) — the wizard
        itself surfaces that as a normal error notification.

        Returns True when this call fully exhausts every remaining unit
        for this order (nothing left afterward at all) — the caller uses
        this to decide between meli_stock_return_state 'done' and
        'partial'.
        """
        self.ensure_one()
        __, transit_moves = self._meli_quarantine_transit_pickings()
        new_pickings = self.env['stock.picking']
        for product, quantity in quantities_by_product.items():
            if quantity <= 0:
                continue
            remaining_qty = quantity
            candidate_moves = transit_moves.filtered(
                lambda m: m.product_id == product
            )
            for move in candidate_moves:
                if remaining_qty <= 0:
                    break
                already_moved_on = sum(
                    move.returned_move_ids.filtered(
                        lambda m: m.state == 'done'
                    ).mapped('quantity')
                )
                move_remaining = move.quantity - already_moved_on
                if move_remaining <= 0:
                    continue
                take_qty = min(move_remaining, remaining_qty)
                return_wizard = self.env['stock.return.picking'].with_context(
                    active_ids=move.picking_id.ids, active_id=move.picking_id.id,
                    active_model='stock.picking',
                ).create({
                    'location_id': location_id,
                    'picking_id': move.picking_id.id,
                    'product_return_moves': [(0, 0, {
                        'product_id': product.id,
                        'quantity': take_qty,
                        'move_id': move.id,
                        'uom_id': product.uom_id.id,
                    })],
                })
                new_picking_id, __ = return_wizard._create_returns()
                new_picking = self.env['stock.picking'].browse(new_picking_id)
                # Fix 2026-09-25 (user-directed): a plain
                # stock.return.picking copy() keeps the ORIGINAL move's
                # own procurement group — which is this sale's own
                # group (the same reason stock.picking.sale_id resolves
                # at all, see _meli_apply_partial_cancellation's own
                # docstring on this exact mechanism). Left alone, this
                # quarantine-bound transfer would show up as one of the
                # sale's own pickings — confusing next to the real
                # delivery/invoicing picture, and with no bearing on
                # qty_delivered/qty_invoiced at all. Cleared here, right
                # after creation and before validation, so it stands on
                # its own; the sale's own chatter link below is the only
                # trace connecting the two.
                new_picking.group_id = False
                new_picking.move_ids.group_id = False
                result = new_picking.button_validate()
                if isinstance(result, dict):
                    raise UserError(_(
                        "Automatic validation of the quarantine transfer "
                        "%s needs manual confirmation (e.g. insufficient "
                        "stock) — review and apply it manually, then try "
                        "again for what's left."
                    ) % new_picking.name)
                new_pickings |= new_picking
                remaining_qty -= take_qty
            if remaining_qty > 0:
                raise UserError(_(
                    "Only %(available)s of %(product)s is left in "
                    "transit for this order — cannot move %(requested)s."
                ) % {
                    'available': quantity - remaining_qty,
                    'product': product.display_name,
                    'requested': quantity,
                })
        # Fix 2026-09-25 (user-directed): links straight to each
        # transfer via chatter (same generic record-link mechanism this
        # module's own _meli_post_with_mention already uses for a
        # partner @-mention, here pointed at stock.picking instead) —
        # never as one of this sale's own pickings (see the group_id
        # clearing above for why), so this is the only trace connecting
        # the two.
        picking_links = ', '.join(
            f'<a href="#" data-oe-model="stock.picking" data-oe-id="{picking.id}" '
            f'class="o_mail_redirect">{picking.name}</a>'
            for picking in new_pickings
        )
        self.message_post(body=_(
            "Physical return confirmed to quarantine location "
            "%(location)s via transfer(s) %(pickings)s."
        ) % {
            'location': self.env['stock.location'].browse(location_id).display_name,
            'pickings': picking_links,
        })
        return not any(self._meli_quarantine_remaining_by_product().values())

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

        Fix 2026-09-16 (real production bug, order S840530/pack
        2000014910327557): _meli_apply_partial_cancellation's own stock
        return has no dependency of its own on this sibling's 'sale'
        invoice document ever having arrived — it returns whatever was
        delivered regardless. If that document already exists locally
        (matched via meli_order_id/sale_order_id) but was never invoiced
        yet, returning the stock FIRST nets its own delivered quantity
        back to 0 (same mechanics as _meli_recover_cancelled_on_arrival_
        full's own identical fix), permanently blocking
        _create_invoices() under a 'delivered quantities' policy —
        there is nothing left to invoice once the return already
        happened. Calling the shared reconciler here first (idempotent,
        safe to call repeatedly — see its own docstring) creates/relates
        whatever invoice already has a document waiting, before any
        stock is touched. This can't help when Mercado Libre's own
        'sale' document for this sibling hadn't even reached Odoo YET at
        this exact moment (a real, separate timing gap on Mercado
        Libre's own side, confirmed for S840530 itself — its credit note
        arrived a full 20 minutes before its own sale document did) —
        nothing at the call-order level can invoice a document that
        doesn't exist locally yet. This fix only closes the "document
        already here, just not invoiced" half of the gap.
        """
        self.ensure_one()
        self._meli_reconcile_invoicing()
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

    def _meli_sibling_delivered_and_returned_qty(self, lines):
        """Total delivered vs. returned quantity for a pack sibling's own
        line(s), read straight from stock moves — never from
        sale.order.line.product_uom_qty, which this module's own
        automation deliberately never edits (see
        _meli_apply_partial_cancellation's own Quantity comment: Fix
        2026-09-15). Shared by that method and by
        _meli_sibling_is_fully_cancelled, so both agree on the exact
        same definition of "fully returned".
        """
        done_pickings = self.picking_ids.filtered(
            lambda p: p.state == 'done' and p.picking_type_id.code == 'outgoing'
        )
        sibling_moves = done_pickings.move_ids.filtered(
            lambda m: m.sale_line_id in lines and m.state == 'done'
        )
        delivered_qty = sum(sibling_moves.mapped('quantity'))
        returned_qty = sum(
            sibling_moves.mapped('returned_move_ids').filtered(
                lambda m: m.state == 'done'
            ).mapped('quantity')
        )
        return delivered_qty, returned_qty

    def _meli_apply_partial_cancellation(self, cancelled_order_id):
        """The core work of a partial cancellation, without the pack-
        closure check — see _meli_process_partial_cancellation's own
        docstring for why that check is kept separate and only runs
        from there. Starts from THIS sibling's own line(s) — found via
        sale.order.line.meli_order_id — to build/find its own portion
        of the invoice's credit note (see _meli_relate_partial_
        cancellation_credit_note: a line-scoped, hand-built credit
        note — never the shared, whole-invoice account.move.reversal
        wizard _meli_reconcile_invoicing uses for a TOTAL cancellation,
        which mirrors EVERY line of the source invoice and would
        over-refund any OTHER sibling sharing that same consolidated
        invoice). The physical stock return that follows is then scoped
        to whatever that credit note ACTUALLY ends up covering — usually
        just this same sibling, but Mercado Libre can file one
        devolución document whose own concepts span more than one
        individual order in the pack (Fix round 9 — see the inline
        comment where `lines` gets reassigned below), in which case that
        other sibling's own delivered stock is returned here too. The
        sale itself is never cancelled directly by this method (that's
        _meli_close_pack_if_every_sibling_cancelled's job, once every
        sibling reaches its own final state), and — Fix 2026-09-15 —
        no line's own quantity/amount is ever touched either (see the
        Quantity comment below): only stock and credit notes reflect
        the cancellation.

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

        # Fix 2026-09-16 (user-directed follow-up, real production case
        # S840530): if there is no posted invoice yet for this order,
        # the credit-note step just above already found that out and
        # posted its own "no posted invoice yet to apply it to — review
        # manually" chatter message — but, until this fix, this method
        # returned the sibling's own stock ANYWAY, regardless. That
        # nets this sibling's own delivered quantity back to 0, so once
        # Mercado Libre's own 'sale' document for it finally does
        # arrive (whether by webhook, invoices poll, or an Excel
        # import/relate batch — this gate doesn't care which), Odoo can
        # never invoice it under a 'delivered quantities' policy — the
        # exact, permanent dead end S840530 itself hit. Deferring the
        # RETURN too (not just the credit note) until an invoice
        # genuinely exists means whatever re-triggers this method next
        # (_cron_retry_unapplied_documents already treats an unrelated
        # pack credit-note document as unapplied regardless of order —
        # see that cron's own docstring) can still complete the whole
        # job in the right order once the missing piece shows up:
        # invoice, then credit note, then physical return.
        source_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        )
        if not source_invoice:
            return

        # Fix 2026-09-22 round 8 (real production bug, order S978385/
        # pack 2000015158767125): credit_note can ALSO come back empty
        # here even though source_invoice exists — e.g. the document's
        # own concepts couldn't be confidently matched yet (its own
        # "pendiente por descuadre venta y factura" chatter message,
        # meli_needs_manual_mismatch_review — see _meli_relate_partial_
        # cancellation_credit_note's own docstring). Physically
        # returning this sibling's stock regardless would leave a real,
        # serious inconsistency: the product is back in the warehouse,
        # but the customer's own invoice still shows the full charge
        # with no credit ever applied — confirmed exactly this way in
        # production (stock returned via ML/RET/37878, zero out_refund
        # ever created). The whole point of relating the credit note
        # FIRST (see this method's own docstring above) is defeated if
        # the return proceeds anyway when that step didn't actually
        # succeed. Naturally retried later, same as the "no posted
        # invoice yet" case above: once the concepts resolve (e.g. a
        # missing sibling line gets added, or a future code fix widens
        # the matching), this method runs again and completes both
        # halves together.
        if not credit_note:
            return

        # Fix 2026-09-22 round 9 (user-directed, real production bug,
        # order S978385/pack 2000015158767125): Mercado Libre can file
        # ONE devolución document whose own concepts span MULTIPLE
        # individual orders within the same pack — confirmed here: the
        # XML total ($497.30) equals the ENTIRE invoice's own total, with
        # one concept for THIS notified sibling (JCP03) and one for a
        # DIFFERENT sibling (CJC01) that was never itself reported
        # 'cancelled'. _meli_relate_partial_cancellation_credit_note
        # already matches and credits every one of those concepts
        # against the sale's own lines regardless of which sibling
        # triggered this call (see its own docstring: "matched across
        # the WHOLE pack's invoice, not just cancelled_order_id's own
        # sibling") — but the physical return below used to stay scoped
        # to `lines` (this one sibling only), leaving the other
        # sibling's own already-delivered stock never returned even
        # though its product was already fiscally credited in the very
        # same credit note. The credit note actually built is the
        # ground truth for what Mercado Libre is reversing — the return
        # must cover whatever it covers. Falls back to the original,
        # narrower `lines` when the credit note has no product line(s)
        # of its own to key off (e.g. a discount-only line, which
        # deliberately carries no sale_line_ids — see Fix round 5's own
        # comment below), preserving the old, single-sibling behaviour
        # in every other case.
        credited_lines = credit_note.invoice_line_ids.mapped('sale_line_ids') & self.order_line
        lines = credited_lines or lines

        # ---- Inventory: return every delivered move belonging to
        # whichever line(s) this credit note actually covers (see the
        # comment above — usually just this sibling's own line(s), but
        # not always). Same by-code stock.return.picking pattern as
        # _meli_return_full_pickings.
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

        # ---- Quantity: deliberately NEVER touched. Fix 2026-09-15 (real
        # production bug, order 2000018329804996): this used to zero
        # line.product_uom_qty down to what was actually still delivered
        # — but that changes the sale's own amount_total/amount_untaxed,
        # and a line already reduced to 0 before it was ever invoiced can
        # never appear on any invoice _create_invoices() later generates,
        # permanently blocking _meli_relate_partial_cancellation_credit_
        # note (stuck forever on "no posted invoice yet"/"product line(s)
        # could not be found on invoice"). Mercado Libre's own devolución
        # must only ever produce a credit note + a stock return — the
        # sale's own amounts/quantities are never this automation's to
        # edit. Per-line delivered/returned quantities are still computed
        # here (from moves, never from product_uom_qty) — needed for the
        # chatter message below and for _meli_sibling_is_fully_cancelled's
        # own equivalent, qty-write-free check.
        per_line_delivered_returned = {}
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
            per_line_delivered_returned[line] = (delivered_qty, returned_qty)
            total_delivered_qty += delivered_qty
            total_returned_qty += returned_qty

        # ---- Chatter: sale.order.line has no chatter tracking of its
        # own (Global Constraints), so the whole outcome — product,
        # quantity actually returned (if any), and credit note — is
        # reported here, on the order. Fix Round 2, Important #1: the
        # three distinct cases below are told apart by ACTUAL
        # delivered/returned state (total_delivered_qty/
        # total_returned_qty), never by whether THIS call created a new
        # picking (new_pickings) — that used to conflate "never delivered"
        # with "already delivered and already returned earlier", wrongly
        # claiming "transfer never completed" on an idempotent replay of
        # an already-fully-processed sibling. Fix 2026-09-15: reports
        # delivered/returned quantities, NOT "now %(qty)s" — the line's
        # own product_uom_qty is deliberately never changed anymore (see
        # the Quantity comment above), so it would just repeat the
        # order's original, unaffected ordered quantity here.
        product_lines = ', '.join(
            _("%(product)s (entregado %(delivered)s, devuelto %(returned)s)") % {
                'product': line.product_id.display_name,
                'delivered': per_line_delivered_returned[line][0],
                'returned': per_line_delivered_returned[line][1],
            }
            for line in lines
        )
        # Fix 2026-09-22 round 9 (see the `credited_lines`/`lines`
        # reassignment above): once the credit note's own concepts span
        # more than just the notified sibling, the "other sibling(s)
        # left completely untouched" wording below would be flatly
        # false — this pack DID have another individual order's own
        # stock/credit touched, by the very same document. Reported as
        # its own extra sentence rather than rewriting every branch's
        # wording inline.
        other_covered_sibling_ids = (
            set(lines.mapped('meli_order_id')) - {False, cancelled_order_id}
        )
        if not total_delivered_qty:
            # (a) Never delivered at all — nothing physical to return.
            message = _(
                "Mercado Libre order %(order_id)s — one individual order "
                "within this active pack — was cancelled. It had no "
                "delivered stock to return (its own outbound transfer "
                "never completed) — its own line's ordered quantity/"
                "amount was left untouched (never edited by this "
                "automation); the sale itself and this pack's other "
                "sibling(s) were left completely untouched."
                "<br/>Line(s): %(product_lines)s."
            ) % {'order_id': cancelled_order_id, 'product_lines': product_lines or '-'}
        elif new_pickings:
            # (c) Delivered, and returned just now, in this very call.
            message = _(
                "Mercado Libre order %(order_id)s — one individual order "
                "within this active pack — was cancelled/returned. Only "
                "its own stock was returned; the sale itself and this "
                "pack's other sibling(s) were left completely untouched."
                "<br/>Line(s): %(product_lines)s."
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
        if other_covered_sibling_ids:
            message += '<br/>' + _(
                "The related credit note's own concepts also covered "
                "%(other_ids)s — a DIFFERENT individual order within "
                "this same pack, never itself reported 'cancelled' — so "
                "its own delivered stock was returned here too, since "
                "Mercado Libre already credited it in the same document."
            ) % {'other_ids': ', '.join(sorted(other_covered_sibling_ids))}
        if credit_note:
            message += '<br/>' + _(
                "Credit note %s covers this document's own portion of "
                "the invoice."
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
        # Real production bug (2026-09-17, 14 real orders confirmed via
        # direct DB query — S841211/S841118/S841037/S841005/S841000/
        # S840715/... among others): a single-sibling pack whose line(s)
        # were never stamped with their own meli_order_id (the same gap
        # _meli_sibling_lines already falls back for — see that method's
        # own docstring) resolves to an EMPTY sibling_ids here, even
        # though _meli_apply_partial_cancellation just successfully
        # returned that sibling's stock and applied its credit note
        # moments ago (via the exact same fallback). Without this, the
        # "every sibling cancelled" check never even starts, and the sale
        # is silently left open forever — stock returned, credit note
        # applied, but never cancelled. Mirrors _meli_sibling_lines's own
        # fallback exactly: no line carries any meli_order_id at all
        # means there is, by definition, only one individual order
        # involved, which is self.meli_order_id itself.
        if not sibling_ids and self.meli_order_id and not any(
            self.order_line.mapped('meli_order_id')
        ):
            sibling_ids = {self.meli_order_id}
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
        - every unit ever delivered for its own line(s) has since been
          returned (or nothing was ever delivered at all), AND
        - it already has its own live (non-cancelled) credit note
          related.

        Fix 2026-09-15: used to check line.product_uom_qty == 0 instead
        — but this module's own automation no longer ever writes that
        field (see _meli_apply_partial_cancellation's own Quantity
        comment: a line zeroed before it was ever invoiced permanently
        blocked its own credit note from ever finding a matching invoice
        line). Delivered-vs-returned, read straight from stock moves via
        _meli_sibling_delivered_and_returned_qty, is the equivalent
        signal that needs no write of its own — a line can legitimately
        have nothing delivered yet (see _meli_apply_partial_cancellation's
        own "never delivered" case) while the credit note itself is still
        pending from Mercado Libre, which is exactly why the credit-note
        check below is still required too: the whole pack only gets
        auto-cancelled once the fiscal side is genuinely settled too,
        matching this module's existing carefulness around anything
        credit-note-adjacent (see, e.g., the double-refund guards in
        _meli_reconcile_invoicing and
        _meli_relate_partial_cancellation_credit_note).
        """
        self.ensure_one()
        lines = self._meli_sibling_lines(sibling_id)
        if not lines:
            return False
        delivered_qty, returned_qty = self._meli_sibling_delivered_and_returned_qty(lines)
        if returned_qty < delivered_qty:
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
        manager_partner_ids = set()
        if group:
            managers = self.env['res.users'].sudo().search([
                ('groups_id', '=', group.id),
                ('company_id', 'in', self.company_id.ids),
            ])
            manager_partner_ids.update(managers.mapped('partner_id').ids)
        # Fix 2026-09-14: also notify this company's configured
        # "Responsables de Fallos" (meli.config.failure_notify_user_ids)
        # — see _meli_post_with_mention's identical fix for the full
        # rationale (a systems/ops team, not just whoever holds the
        # Queue Job Manager permission, needs to see these).
        config = self.env['meli.config'].sudo().search(
            [('company_id', '=', self.company_id.id)], limit=1,
        )
        manager_partner_ids.update(config.failure_notify_user_ids.mapped('partner_id').ids)
        manager_partner_ids = list(manager_partner_ids)
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

    def _meli_post_with_mention(self, body, mention_partner=None, notify_failure_team=True):
        """Posts a chatter message with a real, visible @-mention — the
        same HTML Odoo itself generates when a person types '@Name' in
        the composer — instead of just a silent notification, so it
        reads unmistakably as "hey, you" in the chatter, not just an
        entry someone might scroll past.

        Defaults to the order's salesperson. Pass `mention_partner`
        explicitly to mention someone else instead (e.g. the configured
        returns manager for a Mercado Libre claim, in meli_claim.py).

        Fix 2026-09-16 (user-directed follow-up): `notify_failure_team`
        defaults to True — matching every ACTUAL error/needs-review call
        site this method has (a confirm failure, an unmapped SKU, an
        amount mismatch, etc.) — but callers reporting something that
        merely NEEDS EYES ON IT, without anything having actually failed
        (e.g. "this order was adopted from Ventiapp, here's what that
        means"), must pass False: the "Responsables de Fallos"
        (meli.config.failure_notify_user_ids) field is specifically for
        failures, and blasting it on every informational note would
        train that team to tune the channel out.
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
        notify_partner_ids = set(partner.ids)
        if notify_failure_team:
            # Fix 2026-09-14: also notify this company's configured
            # "Responsables de Fallos" (meli.config.failure_notify_user_ids)
            # on every one of these "an automation needed a human"
            # messages — before this, only the order's own salesperson
            # (or an explicit mention_partner) ever saw it, so a
            # systems/ops team had no reliable way to be looped in. Same
            # mail_post_autofollow=False guard _meli_notify_queue_job_
            # managers already uses: these are one-off notifications,
            # not a permanent chatter subscription to every Mercado
            # Libre order.
            config = self.env['meli.config'].sudo().search(
                [('company_id', '=', self.company_id.id)], limit=1,
            )
            notify_partner_ids.update(config.failure_notify_user_ids.mapped('partner_id').ids)
        self.with_context(mail_post_autofollow=False).message_post(
            body=mention + body, partner_ids=list(notify_partner_ids),
        )

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
                        # Fix 2026-09-24 (real production bug, orders
                        # 993840/994428, user-directed): a real
                        # PostgreSQL error inside the reconciler (e.g. a
                        # NOT NULL violation) aborts the transaction —
                        # this method used to call
                        # _meli_recover_aborted_transaction() here, which
                        # did a bare self.env.cr.rollback(). That's a
                        # FULL transaction rollback, not a scoped one —
                        # nested inside queue_job_cron_jobrunner's own
                        # per-job savepoint (this whole method runs
                        # inside a queue.job), it silently invalidated
                        # THAT outer savepoint too. The job's own later
                        # attempt to roll back to it then raised
                        # InvalidSavepointSpecification, cascading into
                        # InFailedSqlTransaction and leaving the job
                        # permanently stuck 'pending' — blocking every
                        # other job behind it in the queue, confirmed
                        # exactly this way in production. A `with
                        # self.env.cr.savepoint():` here instead performs
                        # a properly SCOPED rollback (ROLLBACK TO
                        # SAVEPOINT, not a bare ROLLBACK) on failure,
                        # restoring a clean, postable transaction state
                        # without touching anything outside this
                        # method's own boundary — the chatter message
                        # below still posts fine either way.
                        with self.env.cr.savepoint():
                            self._meli_reconcile_invoicing()
                    except Exception:
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
        # Fix 2026-09-22 (user decision): a real @-mention to
        # meli.config.returns_manager_id (Pedro Cortez), not just a
        # silent chatter note nobody is pinged for — every one of the
        # three MELI_STATUS_CHANGE_ALERTS statuses this fallback can be
        # reached for ('partially_refunded', 'pending_cancel', or a
        # 'cancelled' this connector doesn't/can't auto-process) is a
        # real return/refund matter, not a technical failure —
        # notify_failure_team=False on purpose (see _meli_post_with_
        # mention's own docstring: this is "needs eyes", not "something
        # failed").
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        self._meli_post_with_mention(
            message, mention_partner=config.returns_manager_id.partner_id,
            notify_failure_team=False,
        )

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

        Fix 2026-09-16 (real regression: a return-on-arrival recovery
        rolled back its OWN just-created invoice and credit note):
        _meli_return_full_pickings() below validates a real transfer,
        which makes stock_picking's _action_done() override call
        order._meli_reconcile_invoicing() again on THIS SAME order,
        before this method itself even finishes — reachable both when a
        caller invokes this method directly (_meli_flag_status_change,
        _meli_recover_cancelled_on_arrival_full) and when
        _meli_reconcile_invoicing's own credit-note step invokes it. That
        nested reconcile call would see meli_auto_cancellation_processed
        still False (this method hasn't reached that line yet) and
        recursively call THIS method a second time — a genuine second
        action_cancel() on an order its own recursive call already
        cancelled, which raises and unwinds the caller's enclosing
        savepoint, discarding everything the recursion itself already
        created. Setting the context flag here (not just inside
        _meli_reconcile_invoicing) covers BOTH call shapes, since every
        picking recordset this method's own return touches inherits this
        same self.env/context.
        """
        self.ensure_one()
        self = self.with_context(meli_reconciling_invoicing=True)
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
        # Fix 2026-09-19 (user-directed follow-up, real production case
        # S842981/pack 2000015098921997): a pack order recovered here
        # used to be degraded straight to manual review — originally
        # (2026-09-09) because a LATER, genuinely-paid sibling arriving
        # on an already-cancelled order had no safe path of its own:
        # _meli_add_pack_sibling_lines would add its line, but nothing
        # would ever deliver/return/invoice it (Odoo's own stock-rule
        # mechanism silently skips any line whose order isn't 'sale').
        # That gap is closed now — see _meli_force_deliver_cancelled_
        # sibling_line's own docstring — so a sibling discovered after
        # this pipeline runs is no longer silently lost; it gets its
        # own full delivery/return/invoice/credit-note treatment
        # retroactively, exactly like any other sibling. Running the
        # same whole-order recovery pipeline a non-pack order already
        # gets is therefore safe for a pack too.
        orphaned_documents = self.env['meli.invoice.document'].sudo().search([
            ('meli_order_id', 'in', (
                [self.meli_pack_id, self.meli_order_id] if self.meli_pack_id
                else [self.meli_order_id]
            )),
            ('sale_order_id', '=', False),
        ])
        if orphaned_documents:
            orphaned_documents._compute_sale_order_id()

        # Fix 2026-09-16 (real regression uncovered by fixing the
        # reentrancy bug above): the ORIGINAL sale invoice (Mercado
        # Libre's own 'sale' document, arriving alongside the
        # 'devolution' one for an order cancelled this early) must be
        # created and related BEFORE any stock return happens — Odoo's
        # own _create_invoices() refuses once qty_delivered has been
        # netted back to 0 by the return, and separately refuses once
        # the order itself reaches 'cancel' (_get_invoiceable_lines only
        # considers orders in 'sale'/'done'). Both become permanently
        # true the moment Phase 1 (stock return + action_cancel()) below
        # runs — there is no invoicing this order the normal way
        # afterward. Reusing _meli_reconcile_invoicing() here (instead of
        # some new invoice-only helper) is safe and not redundant with
        # Phase 1/2 below: if a credit-note document is ALSO already
        # available, this same call's own credit-note step will already
        # discover (via the live status check) that the order is
        # cancelled and run the WHOLE pipeline itself (Phase 1 included)
        # — meli_auto_cancellation_processed being True right after is
        # exactly how the guard below recognizes that and skips a
        # redundant, second Phase 1 call (which would otherwise
        # action_cancel() an already-cancelled order and raise).
        try:
            with self.env.cr.savepoint():
                self._meli_reconcile_invoicing()
        except Exception:
            # Fix 2026-09-24 (real production bug, orders 993840/994428):
            # this used to also call _meli_recover_aborted_transaction()
            # here — a bare self.env.cr.rollback() (FULL transaction
            # rollback), redundant with — and far more destructive than
            # — the `with self.env.cr.savepoint():` above, which already
            # rolls back cleanly to just this method's own boundary the
            # instant the exception happens. That extra full rollback
            # corrupted queue_job_cron_jobrunner's own outer per-job
            # savepoint, which doesn't exist to roll back to anymore by
            # the time the job runner itself tries — the exact
            # InvalidSavepointSpecification/InFailedSqlTransaction
            # cascade confirmed in production, leaving the job stuck
            # 'pending' forever and blocking everything queued behind
            # it. See this method's own Phase 2 equivalent below for the
            # same fix, and _meli_flag_status_change's matching one.
            _logger.exception(
                "Mercado Libre order %s: could not create/relate this "
                "order's own invoice before automatic cancellation-on-"
                "arrival processing — the order stays as created, "
                "confirmed, and delivered, but needs manual review.",
                self.client_order_ref,
            )
            self._meli_notify_queue_job_managers(_(
                "Mercado Libre reported this order as already cancelled "
                "when it was first imported. It was created, confirmed, "
                "and delivered as a precaution, but its invoice could not "
                "be created automatically — review manually whether to "
                "cancel it and apply any invoice/credit note."
            ))
            return
        if self.meli_auto_cancellation_processed:
            # The call above already discovered, on its own, that this
            # order is genuinely cancelled (via an already-available
            # credit-note document) and ran the entire pipeline itself —
            # nothing left to do.
            self._meli_notify_queue_job_managers(_(
                "Mercado Libre reported this order as already <b>cancelled</b> "
                "when it was first imported. It was created, confirmed, and "
                "delivered as a precaution, then automatically cancelled and "
                "reconciled."
            ))
            return

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
            # Fix 2026-09-24: same scoped-savepoint fix as the try/except
            # above — see its own comment for the full story (real
            # production bug, orders 993840/994428). This call used to
            # run unwrapped, with _meli_recover_aborted_transaction()'s
            # bare self.env.cr.rollback() as the only recovery on
            # failure — a full transaction rollback that corrupted
            # queue_job_cron_jobrunner's own outer per-job savepoint.
            with self.env.cr.savepoint():
                self._meli_reconcile_invoicing()
        except Exception:
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

    def _meli_invoice_document_amount_mismatches(self, invoice_document):
        """Whether invoice_document's own XML total disagrees with this
        order's own amount_total by more than 5 cents — same tolerance
        meli.invoice.document._compute_meli_amount_mismatch already uses.
        Extracted into its own method (2026-09-18) purely so tests
        written before this gate existed — none of which ever needed
        their fixture's fake XML total to genuinely match their order's
        real amount, since nothing checked that before — can mock this
        one call instead of rebuilding each of their own fixtures for
        real; see _meli_reconcile_invoicing's own Fix 2026-09-18 comment
        for why the gate itself exists.

        Fix 2026-09-22 (real production case, order S974870/pack
        2000015124781237): also True when the document's OWN already-
        related move doesn't add up to its XML total
        (meli_move_amount_mismatch) — the sale-vs-XML check above can
        stay clean even when the actual posted invoice/credit note is
        genuinely short (e.g. a pack sibling's line wasn't delivered
        yet when the invoice was first built, then got added to
        order_line later without ever re-triggering a rebuild). Without
        this, invoice_up_to_date would keep considering a document like
        that "fine" forever, since only the sale total was ever
        compared, never what was actually invoiced/credited.
        """
        self.ensure_one()
        return bool(
            abs(invoice_document.meli_xml_total - self.amount_total) > 0.05
            or invoice_document.meli_move_amount_mismatch
        )

    def _meli_correct_move_from_document(self, move, document):
        """Corrects an ALREADY-POSTED invoice/credit note's own line(s)
        to match every concept its own meli.invoice.document's CFDI
        reports, without ever touching this order's own invoicing
        policy or going through _create_invoices()/_get_invoiceable_
        lines() at all.

        2026-09-22, user-directed: the real gap (order S974870/pack
        2000015124781237) is that by the time this needs correcting the
        order is already 'cancel' — Odoo's own _get_invoiceable_lines()
        only considers orders in 'sale'/'done' and nets delivered
        quantity back to 0 on a return, so _meli_reconcile_invoicing's
        normal refacturación path ("No hay artículos disponibles para
        facturar") can never rebuild an invoice for it, REGARDLESS of
        invoicing policy. This never calls _create_invoices() at all —
        it only edits the move that already exists.

        Resets move to draft — breaking payment reconciliation and the
        l10n_mx_edi 'sent' state first, if needed, via the exact same
        local-bookkeeping trick _meli_cancel_account_move_breaking_
        reconciliation already uses for button_cancel() (button_draft()
        is gated by the identical need_cancel_request check, confirmed
        in core account.move.button_draft()) — adds whatever product
        line(s) the document's own concepts name that aren't already on
        move (matched by product name against self.order_line, same
        technique _meli_relate_partial_cancellation_credit_note already
        uses), re-posts, and re-applies the payment reconciliation it
        had before. Never removes/reduces an existing line, only adds
        what's missing — this is strictly additive correction.

        Returns True if anything was actually added/corrected, False
        when there's nothing to do (no XML, no concepts, every concept
        already matched, a concept couldn't be confidently matched to
        exactly one line — that one posts its own manual-review message
        and is skipped, never guessed — or every concept matched but the
        resulting total still wouldn't reconcile with the document's own
        XML total).

        2026-09-22 (user-directed, real case: document 3000000092741387
        reports "Tóper De Vidrio Styrka Herméticos Con Tapa 4 Unidades
        Blanco" — Mercado Libre's own marketplace LISTING title — for
        what this catalog calls "[CJC06] JUEGO DE CONTENEDORES DE VIDRIO
        RECTANGULARES 8 PIEZAS"; the two share no words at all, so
        name-matching alone left it unmatched even though it was
        obviously the right line). Three layers of matching are tried,
        in order, per concept: (1) exact product name, (2) the
        product's own internal reference/default_code appearing as a
        whole word somewhere in the concept's own description (the
        same document's OTHER concept, for [CJC02], embeds "Cjc02"
        right in Mercado Libre's own title — a much stronger signal
        than price when it's there), (3) quantity + pre-tax amount
        matching exactly (same $0.05 tolerance used everywhere else in
        this module) among the order lines not already claimed by an
        earlier concept this same run — the only option left when
        neither the name nor the internal code appears anywhere in
        Mercado Libre's own text. As one more safety net on top of all
        three (also user-directed): even after every concept is
        matched, the resulting invoice total is checked against the
        document's own total before anything is actually written — if
        it still wouldn't reconcile, this stops and asks for a human
        instead of posting a correction that doesn't add up.
        """
        self.ensure_one()
        move.ensure_one()
        if not document.xml_file:
            return False
        xml_bytes = base64.b64decode(document.xml_file)
        concepts = document._meli_parse_concepts_from_xml(xml_bytes)
        if not concepts:
            return False

        def _normalize(text):
            return ' '.join((text or '').split()).casefold()

        # Fix 2026-09-22 round 2 (real production regression THIS SAME
        # fix's first version introduced, document 3000000092741387):
        # "is this concept already on the move" must be decided by the
        # candidate's real product_id, never by comparing text —
        # Mercado Libre's own concept description can fail to match an
        # ALREADY-INVOICED line's product name too (not just a missing
        # one), which used to let the price fallback below "discover"
        # that already-invoiced line as if it were missing and add it a
        # second time (real case: concept 1, "Contenedores...Cjc02
        # Gris", never textually matched product [CJC02]'s own name
        # either — it was already on the move, but the old name-only
        # check couldn't tell). Every concept's candidate line is now
        # resolved ONCE (by name first, then by price/quantity), and
        # only AFTERWARD checked against the move's own real product
        # ids — never against normalized text a second time.
        existing_product_ids = set(
            move.invoice_line_ids.filtered(
                lambda l: l.display_type == 'product'
            ).mapped('product_id').ids
        )
        product_lines = self.order_line.filtered(lambda l: l.product_id and not l.display_type)
        missing_lines = self.env['sale.order.line']
        used_lines = self.env['sale.order.line']
        unresolved_concepts = []
        for concept in concepts:
            name_candidates = product_lines.filtered(
                lambda l: _normalize(l.product_id.name) == _normalize(concept['descripcion'])
            )
            if len(name_candidates) == 1:
                candidate = name_candidates
            else:
                # Fallback 1 (2026-09-22, user-spotted, real case:
                # document 3000000092741387's own concept 1,
                # "Contenedores Tóper De Vidrio Herméticos Con Tapa
                # Juego 12 Recipientes 24 Piezas Styrka Cjc02 Gris",
                # never matches product [CJC02]'s own name by text
                # either, but DOES carry that internal reference
                # embedded in Mercado Libre's own marketplace title):
                # the one order line, among those not already claimed
                # by an earlier concept this same run, whose own
                # default_code (internal reference/SKU) appears as a
                # whole word inside the concept's own description —
                # a much stronger signal than price/quantity alone,
                # tried first. \b word boundaries avoid a short code
                # (e.g. "8") coincidentally matching inside an
                # unrelated number in the description.
                code_candidates = (product_lines - used_lines).filtered(
                    lambda l: (
                        l.product_id.default_code
                        and re.search(
                            r'\b' + re.escape(l.product_id.default_code) + r'\b',
                            concept['descripcion'], re.IGNORECASE,
                        )
                    )
                )
                if len(code_candidates) == 1:
                    candidate = code_candidates
                else:
                    # Fallback 2 (2026-09-22, real case: concept 2 of
                    # that SAME document, "Tóper De Vidrio Styrka
                    # Herméticos Con Tapa 4 Unidades Blanco" — Mercado
                    # Libre's own marketplace listing title, carrying
                    # neither this catalog's product name NOR its
                    # internal reference "CJC06" anywhere in the text):
                    # the one remaining order line whose quantity and
                    # pre-tax amount match the concept exactly (same
                    # $0.05 tolerance used everywhere else in this
                    # module).
                    price_candidates = (product_lines - used_lines).filtered(
                        lambda l: (
                            abs(l.product_uom_qty - concept['cantidad']) < 0.001
                            and abs(l.price_unit * l.product_uom_qty - concept['importe']) <= 0.05
                        )
                    )
                    candidate = price_candidates if len(price_candidates) == 1 else None
            if not candidate:
                unresolved_concepts.append(concept)
                continue
            used_lines |= candidate
            if candidate.product_id.id in existing_product_ids:
                continue
            missing_lines |= candidate

        for concept in unresolved_concepts:
            self.message_post(body=_(
                "Mercado Libre document %(document)s reports "
                "'%(product)s', but it could not be matched to "
                "exactly one product line on this sale — "
                "%(move)s was NOT corrected automatically for it. "
                "Review manually."
            ) % {
                'document': document.meli_invoice_id or document.id,
                'product': concept['descripcion'],
                'move': move.name,
            })
        if not missing_lines:
            return False

        existing_subtotal = sum(
            move.invoice_line_ids.filtered(
                lambda l: l.display_type == 'product'
            ).mapped('price_subtotal')
        )
        projected_subtotal = existing_subtotal + sum(
            line.price_unit * line.product_uom_qty for line in missing_lines
        )
        document_subtotal = sum(concept['importe'] for concept in concepts)
        if abs(projected_subtotal - document_subtotal) > 0.05:
            self.message_post(body=_(
                "Mercado Libre document %(document)s's concepts were "
                "matched to product lines on this sale, but the "
                "resulting total (%(projected).2f) still would not "
                "match the document's own total (%(document_total).2f) "
                "— %(move)s was NOT corrected automatically. Review "
                "manually."
            ) % {
                'document': document.meli_invoice_id or document.id,
                'projected': projected_subtotal,
                'document_total': document_subtotal,
                'move': move.name,
            })
            return False

        payment_lines = self.env['account.move.line']
        reconciled_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
            and (l.matched_debit_ids or l.matched_credit_ids)
        )
        for line in reconciled_lines:
            payment_lines |= (
                line.matched_debit_ids.debit_move_id | line.matched_credit_ids.credit_move_id
            ).filtered(lambda l: l.move_id != move)
            line.remove_move_reconcile()
        live_cfdi_document = move.l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_sent'
        )[:1]
        if live_cfdi_document:
            move._l10n_mx_edi_cfdi_invoice_document_cancel(
                live_cfdi_document, MELI_REFACTURA_CANCEL_REASON,
            )
        move.button_draft()
        new_line_vals = []
        for line in missing_lines:
            vals = line._prepare_invoice_line()
            vals['quantity'] = line.product_uom_qty
            new_line_vals.append((0, 0, vals))
        move.write({'invoice_line_ids': new_line_vals})
        move.action_post()
        self._meli_relate_invoice_document(move, document)
        self._meli_reconcile_move_with_payment_lines(move, payment_lines)
        self.message_post(body=_(
            "Corrected %(move)s directly — added missing product "
            "line(s) (%(products)s) so it matches Mercado Libre's own "
            "document %(document)s exactly. This order's own invoicing "
            "policy/route was never touched; only this already-"
            "existing move's own lines were edited."
        ) % {
            'move': move.name,
            'products': ', '.join(missing_lines.mapped('product_id.display_name')),
            'document': document.meli_invoice_id or document.id,
        })
        return True

    def _meli_repair_move_amount_mismatch_now(self):
        """One order's own share of action_meli_repair_move_amount_
        mismatch (2026-09-22 user request, real case order S974870/pack
        2000015124781237) — for every meli.invoice.document already
        flagged meli_move_amount_mismatch (its own already-posted
        invoice/credit note doesn't add up to its real XML total),
        corrects that move directly via _meli_correct_move_from_
        document, without touching this order's own invoicing policy
        or re-running the normal invoice-creation path at all (which
        would fail with "no items available to invoice" for an order
        that's already 'cancel' by the time this runs — see that
        method's own docstring).

        Deliberately takes no company_id argument, unlike every other
        _meli_repair_*_now method in this file — this one never calls
        the Mercado Libre API (no meli.config lookup needed at all), it
        only acts on data already in the database (meli_invoice_
        document_ids, move_ids), so there is nothing here that's ever
        company-scoped.
        """
        self.ensure_one()
        mismatched_documents = self.meli_invoice_document_ids.filtered(
            lambda d: d.meli_move_amount_mismatch
        )
        for document in mismatched_documents:
            move = document.move_ids.filtered(lambda m: m.state == 'posted')[:1]
            if not move:
                continue
            self._meli_correct_move_from_document(move, document)

    def action_meli_repair_move_amount_mismatch(self):
        """Server action (2026-09-22 user request), deliberately NOT
        exposed as a button — same convention as every other repair
        action in this file (wire it up yourself as an
        ir.actions.server: `action = model.action_meli_repair_move_
        amount_mismatch()`).

        Finds every Mercado Libre sale with at least one meli.invoice.
        document flagged meli_move_amount_mismatch (in the current
        selection if any, otherwise model-wide) and enqueues one cheap,
        idempotent job per order via _meli_repair_move_amount_mismatch_
        now.
        """
        if self:
            orders = self
        else:
            orders = self.env['meli.invoice.document'].sudo().search([
                ('meli_move_amount_mismatch', '=', True),
                ('sale_order_id', '!=', False),
            ]).mapped('sale_order_id')
        queued = 0
        for order in orders:
            order.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_move_amount_mismatch_repair_{order.id}",
                description=f"Invoice/CFDI amount mismatch repair check for {order.name}",
            )._meli_repair_move_amount_mismatch_now()
            queued += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"),
                'message': _(
                    "%(queued)s venta(s) puesta(s) en cola para revisión "
                    "de descuadre factura/CFDI — se corrigen solas en "
                    "segundo plano, sin afectar las que ya están "
                    "completas."
                ) % {'queued': queued},
                'type': 'success',
            },
        }

    def _meli_cancel_account_move_breaking_reconciliation(self, move):
        """Cancels move (an already-posted invoice or credit note) even
        when its own receivable/payable line is already reconciled
        against a payment — Mercado Libre orders are typically paid
        immediately, so by the time a stale document needs correcting
        it is very often already reconciled, and Odoo's own
        button_cancel() refuses to cancel a move with a reconciled
        line. 2026-09-19, user-directed, real production case (order
        854722/pack 2000015098921997, a credit note stuck at $378.48
        when Mercado Libre's own XML reports $564.96 for the whole
        pack): "rompes la conciliación si es necesario y después la
        vuelves a generar, sí o sí tenemos que automatizarlo" — this
        never degrades to manual review just because a line is
        reconciled; it always proceeds.

        Returns the payment-side account.move.line(s) that were
        reconciled against move before this ran, so the caller can
        re-apply that exact same reconciliation onto whatever move
        replaces this one (see _meli_reconcile_move_with_payment_lines).
        """
        self.ensure_one()
        move.ensure_one()
        reconciled_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
            and (l.matched_debit_ids or l.matched_credit_ids)
        )
        payment_lines = self.env['account.move.line']
        for line in reconciled_lines:
            payment_lines |= (
                line.matched_debit_ids.debit_move_id | line.matched_credit_ids.credit_move_id
            ).filtered(lambda l: l.move_id != move)
            line.remove_move_reconcile()
        live_cfdi_document = move.l10n_mx_edi_invoice_document_ids.filtered(
            lambda d: d.state == 'invoice_sent'
        )[:1]
        if live_cfdi_document:
            move._l10n_mx_edi_cfdi_invoice_document_cancel(
                live_cfdi_document, MELI_REFACTURA_CANCEL_REASON,
            )
        move.button_cancel()
        return payment_lines

    def _meli_reconcile_move_with_payment_lines(self, move, payment_lines):
        """Re-applies whatever payment reconciliation
        _meli_cancel_account_move_breaking_reconciliation broke off of
        the move it replaced, onto move's own equivalent
        receivable/payable line instead — same counterpart payment(s),
        now pointed at the corrected move.

        Fix 2026-09-22 round 7 (real bug found via the test suite, same
        root cause as the equivalent test-only fixture bug fixed
        earlier the same day): this company's own payment terms can
        split a single invoice's receivable/payable amount across MORE
        THAN ONE line — the old `[:1]` here silently picked only the
        FIRST such line, so whenever the other split's own line was
        the one actually available to reconcile against payment_lines,
        this method reconciled the WRONG (already-settled or
        mismatched) line, then failed later with "You are trying to
        reconcile some entries that are already reconciled" once the
        real target line was reached from elsewhere. And
        matched_debit_ids/matched_credit_ids — the signal this method
        used to decide "already handled" — can read empty on a line
        Odoo's own reconciliation guard (aml.reconciled) already
        considers settled (e.g. a zero-residual rounding split), which
        silently let this method attempt a reconcile Odoo itself was
        always going to refuse. Every account.move.line's own real
        .reconciled field is checked instead, on the FULL set of
        receivable/payable lines this move actually has — never
        limited to just the first one.
        """
        self.ensure_one()
        move.ensure_one()
        if not payment_lines:
            return
        target_lines = move.line_ids.filtered(
            lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable')
            and not l.reconciled
        )
        if not target_lines:
            return
        # Defensive (2026-09-19): a payment line can end up swept into
        # some OTHER reconciliation as a side effect of everything that
        # ran in between capturing it and reaching here (cancelling the
        # stale move, posting the replacement) — reconciling it again
        # would raise instead of silently corrupting anything, but there
        # is nothing left here worth forcing through. Only ever
        # reconciles the lines that are still genuinely free.
        payment_lines = payment_lines.filtered(lambda l: not l.reconciled)
        if not payment_lines:
            return
        (target_lines | payment_lines).reconcile()

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
        # Fix 2026-09-16 (root cause of a real test regression: 2
        # out_refund moves created for a single credit-note document):
        # _meli_process_full_cancellation's own return-picking validation
        # (_meli_return_full_pickings -> button_validate()) makes
        # stock_picking's _action_done() override call THIS SAME method
        # again, on this same order, before the outer call below even
        # returns — this method is not naturally reentrant (the credit-
        # note step's already_related/existing_live_refund checks are
        # only computed ONCE, before the loop, so the outer call has no
        # way to notice the nested call already related the very
        # document it's about to build a second credit note for). This
        # context flag makes any such nested call a no-op; the outer
        # call's own loop already finishes the job (Phase 1 + credit
        # note) once Phase 1 actually returns.
        if self.env.context.get('meli_reconciling_invoicing'):
            return
        self = self.with_context(meli_reconciling_invoicing=True)
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
            MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES,
        )
        meli_invoice_not_usable_statuses = (
            MELI_INVOICE_DEAD_STATUSES | MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES
        )
        # 2026-09-21: {meli.invoice.document id: account.move.line
        # recordset} — populated below if Step 2 has to cancel a stale
        # credit note to unblock refacturación, threaded through to
        # Step 4's own _meli_relate_partial_cancellation_credit_note
        # call so the SAME payment gets re-reconciled onto the
        # corrected credit note that method rebuilds for that document,
        # instead of being silently orphaned.
        credit_note_payment_lines_by_document = {}
        # Fetched once, reused for both the invoice-creation step below
        # and the credit-note step further down (which used to fetch
        # its own, separate copy) — also needed by _meli_invoice_
        # partner_id's own "not resale/1P" fallback.
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', self.company_id.id), ('state', '=', 'connected'),
        ], limit=1)

        # Fix 2026-09-22 (user-directed hardening, real production
        # regression — see _meli_fetch_buyer_shipping_surcharge's own
        # docstring): meli_transaction_type is only ever known once
        # this order's own factura document exists, which can be true
        # from the very first reconcile call onward — checked here,
        # BEFORE any invoice gets (re)built below, so a resale order
        # never has to go through a wasted cancel+recreate cycle to
        # correct itself; a genuine no-op whenever this order isn't
        # 'resale' or has no such line, and self-contained (its own
        # trailing _meli_reconcile_invoicing() call no-ops here via the
        # reentrancy guard above, same as any other nested call).
        self._meli_repair_wrong_shipping_line_now(self.company_id.id)

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
        #
        # Fix 2026-09-18 (real production bug, 20 real orders confirmed,
        # e.g. S967516): ('status', 'not in', MELI_INVOICE_DEAD_STATUSES)
        # was missing here — the credit-note search a few dozen lines
        # down already has it, this one never did. Mercado Libre can
        # deliver a refacturación's OLD (now cancelled) document to
        # Odoo's webhook/import AFTER its own replacement, giving the
        # dead one a LATER create_date than the live one it replaced —
        # confirmed live: both documents shared the exact same fiscal
        # issue_date, only create_date differed. Sorting by create_date
        # alone then picked the dead one as "current", related Odoo's
        # real invoice to it, and every later call saw invoice_up_to_date
        # (below) as already True — silently leaving the genuinely
        # 'authorized' replacement document unapplied forever, with a
        # posted Odoo invoice pointing at a CFDI Mercado Libre itself
        # cancelled.
        invoice_document = self.env['meli.invoice.document'].sudo().search([
            ('sale_order_id', '=', self.id),
            ('transaction_type', 'not in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
            ('xml_file', '!=', False),
            ('status', 'not in', list(meli_invoice_not_usable_statuses)),
        ], limit=1, order='create_date desc, id desc')

        current_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )[:1]
        # Fix 2026-09-20 (user-directed, real production incidents: real
        # confirmed invoices in Odoo left linked forever to a Mercado
        # Libre document that was LATER cancelled/rejected on Mercado
        # Libre's own side — nothing before this ever re-checked an
        # ALREADY-related document's own status again once it was used).
        # Whenever this method re-enters (a status-change webhook for
        # this same document, a manual refresh, another document's own
        # upsert on the same order) and the live invoice's own related
        # document has since died, cancel that invoice automatically —
        # breaking payment reconciliation if it has to, same mechanism
        # already used for a stale credit note — rather than leaving a
        # dead CFDI permanently attached to a confirmed Odoo invoice.
        # Cleared to an empty recordset afterward so the rest of this
        # method proceeds exactly as if there had never been an invoice,
        # picking up any genuinely valid document the normal way below.
        if (
            current_invoice
            and current_invoice.meli_invoice_document_id.status in MELI_INVOICE_DEAD_STATUSES
        ):
            dead_document = current_invoice.meli_invoice_document_id
            dead_invoice = current_invoice
            self._meli_cancel_account_move_breaking_reconciliation(dead_invoice)
            self.message_post(body=_(
                "Mercado Libre's own document %(document)s — the one "
                "invoice %(invoice)s was related to — was "
                "cancelled/rejected on Mercado Libre's own side. The "
                "invoice was cancelled automatically to match; it will "
                "be replaced once a valid document exists."
            ) % {
                'document': dead_document.meli_invoice_id or dead_document.id,
                'invoice': dead_invoice.name,
            })
            current_invoice = self.env['account.move']
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
        # Fix 2026-09-21 (real production case, order S974870/pack
        # 2000015124781237): identity alone ("same document row") is
        # NOT enough — a pack sibling discovered late gets its own
        # line added, but nothing about that alone makes Mercado Libre
        # send a genuinely NEW 'sale' document; the SAME document row
        # can already report the correct, full pack total from the
        # very start (confirmed for order 854722/pack 2000015098921997
        # earlier: one shared invoice XML for the whole pack, filed
        # once). Without this amount check, current_invoice stayed
        # stuck at its own OLD, now-too-small total forever — Step 4's
        # own credit-note concept-matching then also stayed
        # permanently "pendiente" (the missing sibling's own concept
        # can never match a line that was never actually invoiced),
        # even though nothing was actually still missing on the sale
        # itself.
        # Fix round 2 (2026-09-21, real regression this same fix
        # introduced): an inline `abs(current_invoice.amount_total -
        # invoice_document.meli_xml_total) <= 0.05` here — comparing
        # against the INVOICE's own amount instead of the ORDER's —
        # duplicated, unmocked, the ONE amount-mismatch check this
        # whole test suite is deliberately set up to neutralize (see
        # _meli_invoice_document_amount_mismatches's own docstring:
        # TestMeliInvoicingLifecycle.setUp patches THAT method to
        # False for every test except the one that tests it, because
        # every fixture's fake CFDI hardcodes Total="116.00" regardless
        # of the real order under test). The unmocked inline check
        # above almost never matched on THOSE fixtures, so it kept
        # tripping "not up to date" on every idempotent re-entry —
        # cancelling an already-correct invoice and reinvoicing an
        # order that, by then, often had nothing left to invoice
        # (stock already returned / order already cancelled) or
        # producing a second, duplicate invoice. Reusing the existing,
        # already-mockable gate instead keeps this fix's own intent
        # (S974870: a pack sibling discovered late grows the order's
        # real total after the invoice was created against the old,
        # smaller one) — self.amount_total grows exactly when that
        # sibling's line is added — while automatically respecting the
        # same setUp() patch every other call to this gate already
        # does, so none of the ~30 pre-existing fixtures needed to
        # change.
        invoice_up_to_date = bool(
            current_invoice and invoice_document
            and current_invoice.meli_invoice_document_id == invoice_document
            and not self._meli_invoice_document_amount_mismatches(invoice_document)
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
                live_partial_cancellation_credit_notes = self.invoice_ids.filtered(
                    lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
                    and m.meli_invoice_document_id
                    and m.meli_invoice_document_id.transaction_type
                    in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
                )
                for live_partial_cancellation_credit_note in live_partial_cancellation_credit_notes:
                    # Fix 2026-09-21 (real production case, order
                    # S974870/pack 2000015124781237 — user decision):
                    # cancel every stale credit note too — breaking its
                    # own payment reconciliation if it has to, same
                    # mechanism used everywhere else in this file —
                    # instead of permanently blocking. This WAS a
                    # "never guess" safety gate, but real production
                    # evidence shows the actual failure mode isn't
                    # corruption risk, it's a pack sibling discovered
                    # late whose own credit note was built too narrow
                    # (missing that sibling's own concept) before this
                    # invoice ever grew to include it — cancelling
                    # both (every live credit note here, current_
                    # invoice itself right below — see Fix round 3
                    # comment on that unconditional cancel for why it's
                    # no longer an if/else with this block) and letting
                    # Step 4 below rebuild every one of them fresh from
                    # the SAME real documents' own concepts is the fix,
                    # not a manual-review dead end.
                    #
                    # Fix round 2 (2026-09-21, user decision — "deshaces
                    # todo y vuelves a generar"): ALL live credit notes,
                    # not just the first found — a refacturación means
                    # this order's whole invoicing picture is stale, not
                    # just whichever one sibling's credit note happened
                    # to be checked first. Step 4's own rebuild (now
                    # matched against self.order_line, never against
                    # whichever invoice happens to be posted at the
                    # moment — see _meli_relate_partial_cancellation_
                    # credit_note's own comment) can rebuild each one
                    # independently regardless of whether that sibling
                    # still has anything left to invoice.
                    #
                    # Whatever payment was reconciled against each stale
                    # credit note is captured here and threaded through
                    # to Step 4 (see credit_note_payment_lines_by_document
                    # below) so it lands on the corrected credit note
                    # instead of being silently orphaned.
                    stale_credit_note_document = live_partial_cancellation_credit_note.meli_invoice_document_id
                    # Fix round 4 (2026-09-21, real regression this same
                    # segment's own fix introduced): a credit note is
                    # very often reconciled DIRECTLY against the very
                    # invoice it reverses (reversed_entry_id), not
                    # against some separate external payment — Odoo
                    # auto-reconciles exactly that pairing on
                    # action_post() itself. Since current_invoice is
                    # ALSO being cancelled a few lines below (in this
                    # same call, unconditionally now), any "payment
                    # line" captured here that actually belongs to
                    # current_invoice isn't a real payment to carry
                    # forward at all — it's the soon-to-be-cancelled
                    # invoice's own other half. Re-reconciling the
                    # REBUILT credit note against it in Step 4 raised
                    # "You are trying to reconcile some entries that
                    # are already reconciled" (real regression, found
                    # via test_reconcile_refactura_block_does_not_
                    # prevent_step_3_for_another_sibling): excluded
                    # here, there is nothing meaningful left to
                    # restore in that case — the new invoice and new
                    # credit note simply stand on their own, exactly
                    # as if this order had never had a payment applied
                    # to begin with.
                    stale_payment_lines = self._meli_cancel_account_move_breaking_reconciliation(
                        live_partial_cancellation_credit_note,
                    ).filtered(lambda l: l.move_id != current_invoice)
                    if stale_credit_note_document:
                        credit_note_payment_lines_by_document[stale_credit_note_document.id] = (
                            credit_note_payment_lines_by_document.get(
                                stale_credit_note_document.id, self.env['account.move.line'],
                            ) | stale_payment_lines
                        )
                    self.message_post(body=_(
                        "Mercado Libre issued a new invoice document "
                        "(%(new)s) for this order — a prior, now-stale "
                        "credit note (%(credit_note)s) was cancelled "
                        "so the invoice and a corrected credit note "
                        "could both be rebuilt from the real, current "
                        "totals."
                    ) % {
                        'new': invoice_document.meli_invoice_id or invoice_document.id,
                        'credit_note': live_partial_cancellation_credit_note.name,
                    })

                # Refacturación: a newer document exists than the one this
                # invoice reflects. Whether Mercado Libre got here by
                # cancelling+reissuing or by crediting+reissuing, the result
                # on our side is the same: this invoice is stale, cancel it.
                #
                # Fix round 3 (2026-09-21, real regression this same
                # segment's own earlier fix introduced): this used to be
                # the `else` of the `if live_partial_cancellation_credit_
                # note:` block above — current_invoice was left alive and
                # uncancelled whenever a stale credit note ALSO needed
                # cancelling, even though the comment on that block already
                # said "cancelling both". With current_invoice never
                # cancelled, `_create_invoices()` a few lines down found
                # every line already fully invoiced by it (and, once the
                # credit note that used to offset one sibling's line was
                # cancelled, that line even LOOKED over-invoiced) — "No
                # items are available to invoice." Unconditional now: a
                # newer document always makes current_invoice stale,
                # whether or not a credit note also needed cancelling
                # first.
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

            # Fix 2026-09-18 (user decision — real production incidents,
            # 249 records confirmed via meli_amount_mismatch filter):
            # never create a real, CFDI-related invoice for an amount
            # that doesn't match what this order's own lines actually
            # add up to. Most commonly caused by a pack sibling whose
            # line hadn't been added yet when this ran (see
            # _meli_ensure_all_pack_siblings_imported's own docstring) —
            # that fix runs asynchronously, so the race is still
            # possible; this is the safety net for whatever slips past
            # it (that fix, or any other future cause). Same 5-cent
            # tolerance _compute_meli_amount_mismatch already uses.
            # Deliberately does NOT touch the credit-note/stock-return
            # side of this method at all — that side of this same
            # policy is a separate, not-yet-designed piece (2026-09-18
            # conversation): Phase 1 (stock return + cancel) for a Full
            # order already runs earlier and independently of this
            # method (see _meli_flag_status_change ->
            # _meli_process_full_cancellation), so it isn't reachable
            # from here anyway.
            if (
                proceed_with_invoice_step
                and self._meli_invoice_document_amount_mismatches(invoice_document)
            ):
                proceed_with_invoice_step = False
                if not invoice_document.meli_needs_manual_mismatch_review:
                    invoice_document.meli_needs_manual_mismatch_review = True
                    self.message_post(body=_(
                        "Mercado Libre's invoice document %(document)s "
                        "reports a total ($%(xml_total)s) that doesn't "
                        "match this order's own total ($%(order_total)s) "
                        "— the invoice was NOT created automatically. "
                        "Most likely cause: a pack sibling's product is "
                        "still missing from this sale. Review and "
                        "resolve manually, then use \"Retry Invoicing "
                        "Reconciliation\" to create it."
                    ) % {
                        'document': invoice_document.meli_invoice_id or invoice_document.id,
                        'xml_total': '%.2f' % invoice_document.meli_xml_total,
                        'order_total': '%.2f' % self.amount_total,
                    })

            if proceed_with_invoice_step:
                new_invoice = self._create_invoices()
                new_invoice = new_invoice.filtered(lambda m: m.move_type == 'out_invoice')
                # Fix 2026-09-16 (real production bug, order S841000):
                # _create_invoices() otherwise leaves invoice_date at
                # Odoo's own default (today, the day this connector
                # happens to run) instead of the REAL date Mercado
                # Libre's own CFDI reports — see
                # _meli_document_invoice_date's own docstring. Set
                # before action_post(), same as every other business
                # field this method finalizes before posting.
                invoice_date = self._meli_document_invoice_date(invoice_document)
                if invoice_date:
                    new_invoice.invoice_date = invoice_date
                new_invoice.partner_id = config.partner_id.id
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
        # gap this Full-only gate exists to close. (config was already
        # fetched once near the top of this method.)
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
            ('status', 'not in', list(meli_invoice_not_usable_statuses)),
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
            # Fix 2026-09-22 round 3 (real production case, order
            # S955446/pack 2000014868857029 — user-directed correction):
            # this used to block EVERY credit note on a non-Full pack
            # unconditionally, the exact same over-broad gate the
            # non-pack branch above just got fixed for. _meli_relate_
            # partial_cancellation_credit_note below (unlike the fuller
            # _meli_apply_partial_cancellation) only ever RELATES a
            # credit note matching the document's own concepts — it
            # never touches stock/order_line either, so it's just as
            # safe to automate for a confirmed partial refund regardless
            # of Full/non-Full. What still needs Full is a genuine full
            # CANCELLATION of that one sibling: the real stock return for
            # that case runs through a completely separate mechanism
            # (_meli_process_partial_cancellation, triggered by that
            # sibling's own 'cancelled' status-change webhook), which is
            # deliberately still Full-only/paused for non-Full packs —
            # unaffected by this change.
            #
            # A pack can have several siblings in different states at
            # once, so each document's own status has to be checked
            # against ITS OWN individual sibling (document.meli_order_id)
            # — never against self.meli_order_id (only ever the FIRST
            # sibling) or the shared, cached meli_last_status.
            #
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

                # Fix 2026-09-25 (user-directed, real production case,
                # pack 2000018289334426): a non-Full pack sibling can
                # also be genuinely, fully CANCELLED — not just
                # partially refunded — in which case the total-
                # cancellation policy (Phase 1 of the Monterrey XE2
                # project) applies to that ONE sibling: relate the
                # credit note, then return THAT sibling's own delivered
                # stock to the shared transit location, then close the
                # whole pack once every sibling has reached this same
                # state — see _meli_return_sibling_pickings_to_transit
                # and _meli_close_pack_if_every_sibling_cancelled below.
                # Previously this fell through to the same manual-review
                # message as an unconfirmed status, even for a document
                # whose own XML plainly covers the sibling's full amount.
                sibling_is_cancelled = False
                if not is_full:
                    try:
                        live_sibling_data = config._api_get(f'/orders/{cancelled_order_id}')
                    except requests.exceptions.RequestException:
                        self.message_post(body=_(
                            "Mercado Libre generated a credit note "
                            "(%(document)s) for order %(order_id)s (one "
                            "individual order within this pack), but its "
                            "current status could not be confirmed (API "
                            "call failed) — review manually."
                        ) % {
                            'document': document.meli_invoice_id or document.id,
                            'order_id': cancelled_order_id or '?',
                        })
                        continue
                    live_sibling_status = (live_sibling_data or {}).get('status')
                    sibling_is_confirmed_partial_refund = (
                        live_sibling_status == 'partially_refunded'
                    )
                    sibling_is_cancelled = live_sibling_status == 'cancelled'
                    if not sibling_is_confirmed_partial_refund and not sibling_is_cancelled:
                        self.message_post(body=_(
                            "Mercado Libre generated a credit note "
                            "(%(document)s) for order %(order_id)s (one "
                            "individual order within this pack) — review "
                            "manually and apply it; a non-Full pack only "
                            "automates a credit note when Mercado Libre "
                            "confirms a partial refund or a full "
                            "cancellation for that specific order."
                        ) % {
                            'document': document.meli_invoice_id or document.id,
                            'order_id': cancelled_order_id or '?',
                        })
                        continue

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
                sibling_credit_note = self._meli_relate_partial_cancellation_credit_note(
                    sibling_lines, cancelled_order_id,
                    extra_payment_lines_by_document=credit_note_payment_lines_by_document,
                )
                # Fix 2026-09-25 (see this branch's own comment above):
                # only for a genuinely CANCELLED sibling, and only once
                # its credit note actually got related (sibling_credit_
                # note empty means the concepts couldn't be matched yet
                # — nothing to physically return against an unrelated
                # fiscal document, same "never guess" principle as every
                # other credit-note gate in this method). Never for a
                # confirmed partial refund: that never touches stock,
                # by design (see is_confirmed_partial_refund's own
                # handling in the non-pack branch below).
                if sibling_is_cancelled and sibling_credit_note:
                    try:
                        with self.env.cr.savepoint():
                            self._meli_return_sibling_pickings_to_transit(sibling_lines)
                    except Exception:
                        _logger.exception(
                            "Mercado Libre order %s: individual order "
                            "%s (part of this non-Full pack) was "
                            "cancelled and its credit note related, but "
                            "returning its own stock to transit failed "
                            "— needs manual review.",
                            self.client_order_ref, cancelled_order_id,
                        )
                        self._meli_notify_queue_job_managers(_(
                            "Mercado Libre order %(order_id)s (part of "
                            "this non-Full pack) was cancelled and its "
                            "credit note was related, but returning its "
                            "own stock to the transit location failed "
                            "— review manually."
                        ) % {'order_id': cancelled_order_id})
                        continue
                    try:
                        with self.env.cr.savepoint():
                            self._meli_close_pack_if_every_sibling_cancelled()
                    except Exception:
                        _logger.exception(
                            "Mercado Libre order %s: every visible "
                            "sibling in this non-Full pack now appears "
                            "cancelled, but automatically cancelling "
                            "the sale itself failed — needs manual "
                            "review.", self.client_order_ref,
                        )
                        self.message_post(body=_(
                            "Every individual order within this pack "
                            "now appears cancelled, but the sale itself "
                            "could not be cancelled automatically — "
                            "review manually. (This sibling's own "
                            "credit note and stock return above still "
                            "completed successfully.)"
                        ))
            return

        source_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        )[:1]
        # Fix 2026-09-16 (user-directed, real distinction Mercado Libre
        # itself makes): a devolución document does NOT always mean the
        # whole sale was returned — Mercado Libre also issues one for a
        # genuine PARTIAL refund (e.g. a price/damage claim) where the
        # order stays completely normal on their side, nothing physical
        # comes back, and only the fiscal side needs a credit note for
        # that exact amount. The only reliable way to tell these apart
        # is Mercado Libre's own CURRENT order status — checked fresh
        # here, before touching anything, per explicit user instruction
        # ("revises el estatus de la orden de venta... antes de hacer
        # nada"). meli_last_status (this order's own locally-cached
        # status) is NOT trusted for this: it can lag behind reality
        # when the order-status webhook itself is late or lost — exactly
        # the real incident (order S841244) that first exposed this
        # whole gap. A live API failure degrades to manual review; this
        # step never guesses.
        #
        # Fix 2026-09-22 round 2 (user-directed correction): the live
        # status has to be known BEFORE deciding whether the Full-only
        # gate even applies (see below) — a genuine confirmed partial
        # refund never touches stock/order_line at all (see
        # _meli_build_partial_credit_note's own docstring), so there's
        # no physical return for a human to confirm and it's safe to
        # automate regardless of Full/non-Full. Moved up from further
        # below, where it used to run only AFTER a non-Full order had
        # already been unconditionally blocked.
        # Fix 2026-09-25 (real bug, order S953238, VentiApp-adopted):
        # meli_order_id is never filled for an order adopted from
        # VentiApp — only reference/client_order_ref carries Mercado
        # Libre's own id for those (see meli.invoice.document.
        # sale_order_id's own help text on the same gap). Without this
        # fallback, this call built '/orders/False' and 400'd every
        # time, permanently blocking this whole credit-note step for
        # every adopted order — not just a non-Full total cancellation,
        # ANY credit note (including the confirmed-partial-refund case
        # already automated since 2026-09-22).
        live_order_id = self.meli_order_id or self.reference
        try:
            live_order_data = config._api_get(f'/orders/{live_order_id}')
        except requests.exceptions.RequestException:
            self.message_post(body=_(
                "Mercado Libre generated a credit note for this order, "
                "but its current status could not be confirmed (API "
                "call failed) — review manually."
            ))
            return
        live_status = (live_order_data or {}).get('status')
        is_full_cancellation = live_status == 'cancelled'
        # Fix 2026-09-22 (user-directed): the automated partial-refund
        # credit note below only ever applies when Mercado Libre's own
        # LIVE status is specifically 'partially_refunded' — the one
        # status that actually confirms a genuine, resolved partial
        # refund. Any OTHER non-cancelled status (still 'paid',
        # 'pending_cancel' awaiting confirmation, etc.) must NOT be
        # auto-credited just because a devolución document happened to
        # arrive — falls through to the same manual-review message as
        # before.
        is_confirmed_partial_refund = live_status == 'partially_refunded'

        # Fix 2026-09-22 round 2 (user-directed correction, real gap:
        # this used to block EVERY credit note on a non-Full order,
        # including a genuine partial refund — "no es devolución es
        # reembolso parcial... esto sí lo podemos automatizar"): only a
        # genuine FULL cancellation (a real physical return, which needs
        # a human to confirm it for a non-Full order — the ordinary
        # physical-inventory reason every other cancellation automation
        # in this file is Full-only) still requires Full. A confirmed
        # partial refund is automated here regardless of Full/non-Full;
        # anything else not yet confirmed either way (still 'paid',
        # 'pending_cancel', etc.) still falls to manual review too, via
        # this exact same message.
        # Fix 2026-09-24 (user-directed, Monterrey XE2 total-
        # cancellation project, Phase 1): a genuine full cancellation is
        # now automated regardless of Full/non-Full too — see
        # _meli_process_non_full_total_cancellation's own docstring for
        # the non-Full pipeline. Full/non-Full still decides WHICH
        # pipeline runs (and in which order relative to the credit
        # note) a few lines below; it no longer decides WHETHER one
        # runs at all.
        if not is_full_cancellation and not is_confirmed_partial_refund:
            newest_document = credit_note_documents[-1]
            self.message_post(body=_(
                "Mercado Libre generated a credit note (%(document)s) "
                "for this order — review manually and apply it; credit "
                "notes are not automated for non-Full orders."
            ) % {'document': newest_document.meli_invoice_id or newest_document.id})
            return
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
        # Seeded from the order's own persisted flag, not just False:
        # _meli_recover_cancelled_on_arrival_full (and
        # _meli_flag_status_change) already call
        # _meli_process_full_cancellation directly, in a SEPARATE, earlier
        # call, before ever reaching this method's own credit-note step —
        # meli_auto_cancellation_processed is exactly the record of that.
        # Without seeding from it, this step would redundantly call
        # _meli_process_full_cancellation a second time on an
        # already-cancelled order (action_cancel() on a 'cancel' state
        # order raises), which aborted the whole reconciliation
        # transaction in practice, losing the invoice this same call had
        # just created moments earlier.
        full_cancellation_done = self.meli_auto_cancellation_processed
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

            if is_full_cancellation:
                # Fix 2026-09-24 (Monterrey XE2 total-cancellation
                # project, Phase 1): Full runs its own stock-return +
                # action_cancel() pipeline BEFORE the credit note below —
                # none of that inventory ever really left XE's control,
                # so there's nothing to wait for. Non-Full runs the
                # OPPOSITE order: the credit note is built and posted
                # FIRST, and _meli_process_non_full_total_cancellation
                # only runs after — the user's own condition for
                # cancelling ("entregado 0 y facturado 0") can only both
                # be true once the credit note already exists. Either
                # way, only once per call regardless of how many
                # documents this loop iterates (full_cancellation_done),
                # since both pipelines are idempotent by themselves but
                # a second action_cancel() on an already-cancelled order
                # raises.
                if is_full and not full_cancellation_done:
                    # Runs the SAME stock-return + action_cancel() pipeline
                    # the order-status webhook path uses (Phase 1) — here
                    # triggered instead by discovering, via the live API,
                    # that Mercado Libre already considers this order
                    # cancelled, even though no 'cancelled' notification
                    # for it was ever received/processed locally.
                    self._meli_process_full_cancellation(config)
                    self.meli_last_status = 'cancelled'
                    full_cancellation_done = True
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
                # Fix 4 (2026-09-09, user-directed follow-up): account.
                # move.reversal has no invoice_date create field of its
                # own — its `date` field only ever drives the new move's
                # accounting `date`, and reverse_moves() otherwise leaves
                # invoice_date to its ordinary default (today, set once
                # action_post() below runs). The credit note must
                # instead carry the REAL Mercado Libre document's own
                # fiscal date — see _meli_document_invoice_date's own
                # docstring for why that needs converting back through
                # MELI_FISCAL_TIMEZONE, not a naive .date() on the
                # stored (UTC) issue_date. Set before action_post(),
                # same as every other business field this method
                # finalizes before posting.
                credit_note_date = self._meli_document_invoice_date(credit_note_document)
                if credit_note_date:
                    credit_note.invoice_date = credit_note_date
                credit_note.action_post()
                if not is_full and not full_cancellation_done:
                    self._meli_process_non_full_total_cancellation()
                    self.meli_last_status = 'cancelled'
                    full_cancellation_done = True
            elif is_confirmed_partial_refund:
                # Confirmed partial refund: the order stays exactly as
                # it is — never cancelled, its stock never touched
                # (nothing in this branch calls
                # _meli_process_full_cancellation or any stock-return
                # method, on purpose — see this method's own
                # docstring).
                #
                # Fix 2026-09-22 (user decision): re-enabled — was
                # deliberately paused since 2026-09-16, degrading to
                # manual review instead. _meli_build_partial_credit_note
                # never touches stock/order_line, and its own only
                # amount check is per CFDI concept (quantity × that
                # line's own unit price, 5-cent tolerance) — never
                # against this order's own amount_total, which of course
                # won't match a partial refund. Only reached when
                # Mercado Libre's own LIVE status is specifically
                # 'partially_refunded' — never for any other
                # non-cancelled status, where the refund isn't
                # confirmed yet.
                credit_note = self._meli_build_partial_credit_note(
                    source_invoice, credit_note_document,
                )
                if not credit_note:
                    continue
            else:
                # Live status is neither 'cancelled' nor
                # 'partially_refunded' (still 'paid', 'pending_cancel'
                # awaiting confirmation, etc.) — a devolución document
                # existing at all doesn't by itself confirm what Mercado
                # Libre actually intends yet. Degrade to manual review,
                # same "notified" pattern as meli_amount_mismatch_
                # notified so this doesn't repeat every 30 minutes.
                if not credit_note_document.meli_needs_manual_credit_note:
                    credit_note_document.meli_needs_manual_credit_note = True
                    self.message_post(body=_(
                        "Mercado Libre generated a credit note "
                        "(%(document)s) for this order, but its live "
                        "status is '%(status)s' — neither a confirmed "
                        "cancellation nor a confirmed partial refund. "
                        "Review manually."
                    ) % {
                        'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                        'status': live_status or '?',
                    })
                continue
            self._meli_relate_invoice_document(credit_note, credit_note_document)
            credit_note_created = credit_note
            self.message_post(body=_(
                "Credit note %(credit_note)s created and related to "
                "Mercado Libre document %(document)s."
            ) % {'credit_note': credit_note.name,
                 'document': credit_note_document.meli_invoice_id or credit_note_document.id})

    @staticmethod
    def _meli_document_invoice_date(document):
        """Any account.move created from a meli.invoice.document (the
        original sale invoice, a credit note, or a refacturación's
        replacement invoice) must carry THAT document's own issue_date
        as its invoice_date — never Odoo's own default of today (found
        in production, 2026-09-16: an order first delivered/invoiced on
        07/09 showed an invoice dated 15/09, the day this connector
        happened to create it — see meli.invoice.document.issue_date's
        own help text) and never some other, unrelated move's date (the
        old behaviour of the sibling-scoped, hand-built credit-note
        path, Fix 4, 2026-09-09).

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

    def _meli_flag_credit_note_needs_manual_review(self, credit_note_document, body):
        """Shared by _meli_build_partial_credit_note's own three match-
        failure paths: posts the manual-review chatter message and sets
        meli_needs_manual_credit_note (same "notified" pattern as
        meli_amount_mismatch_notified) so _cron_retry_unapplied_
        documents stops re-triggering this same document — without
        this, a genuinely unmatchable partial-refund credit note would
        repost this same message every 30 minutes forever, since it can
        never become is_applied on its own.
        """
        self.message_post(body=body)
        if not credit_note_document.meli_needs_manual_credit_note:
            credit_note_document.meli_needs_manual_credit_note = True

    def _meli_build_partial_credit_note(self, source_invoice, credit_note_document):
        """Builds a real out_refund for a genuine PARTIAL refund — the
        order stays completely normal (see _meli_reconcile_invoicing's
        own is_full_cancellation check, computed from Mercado Libre's
        LIVE order status, never touched here) — crediting EXACTLY the
        product(s) and quantity Mercado Libre's own CFDI reports for
        this devolución (its cfdi:Concepto nodes — see
        meli.invoice.document._meli_parse_concepts_from_xml), matched by
        product name against source_invoice's own lines. Deliberately
        NEVER reverses the whole invoice (account.move.reversal would
        over-credit every other line) and NEVER writes anything on
        self/source_invoice/order_line — 2026-09-16, user-directed:
        "aplicar la nota de crédito normalmente al producto que fue y
        con la cantidad exacta (sin modificar datos de la venta)".

        Returns the created, posted account.move, or an empty
        'account.move' recordset (after posting its own manual-review
        chatter message) when a concept can't be confidently matched to
        one specific invoice line — this never guesses which product a
        real financial document was for.

        Update 2026-09-22 (user-directed, real case: order S955446/
        2000018285646206, document 3000000093287606): a genuine
        DISCOUNT-type partial refund — same quantity, no units
        returned, but Mercado Libre's own CFDI amount is LESS than
        cantidad * the line's own unit price — IS now represented
        safely: the document's own amount is used directly as the
        credited line's price instead of the sale's own full price.
        Matching itself also now falls back to the product's own
        internal reference/SKU, then to the sole remaining unclaimed
        product line, exactly like _meli_correct_move_from_document,
        for when Mercado Libre's own marketplace listing title shares
        no words with this catalog's internal product name.
        """
        self.ensure_one()
        xml_bytes = base64.b64decode(credit_note_document.xml_file)
        concepts = credit_note_document._meli_parse_concepts_from_xml(xml_bytes)
        if not concepts:
            self._meli_flag_credit_note_needs_manual_review(credit_note_document, _(
                "Mercado Libre generated a partial-refund credit note "
                "(%(document)s), but its own CFDI has no readable line "
                "item(s) — review manually."
            ) % {'document': credit_note_document.meli_invoice_id or credit_note_document.id})
            return self.env['account.move']

        product_lines = source_invoice.invoice_line_ids.filtered(
            lambda l: l.display_type == 'product'
        )

        def _normalize(text):
            return ' '.join((text or '').split()).casefold()

        line_vals_list = []
        used_lines = self.env['account.move.line']
        for concept in concepts:
            # Fix 2026-09-22 round 4 (user-directed, real case: order
            # S955446/2000018285646206, document 3000000093287606) —
            # same three extra matching layers _meli_correct_move_
            # from_document and _meli_relate_partial_cancellation_
            # credit_note already use when Mercado Libre's own
            # marketplace listing title shares no words with this
            # catalog's internal product name: (1) exact name (as
            # before), (2) the product's own default_code/SKU
            # appearing as a whole word in the concept's own
            # description, (3) exactly one product line remaining
            # unclaimed by an earlier concept this same run — an
            # unambiguous match regardless of text when there is
            # nothing else it could be.
            matching_lines = product_lines.filtered(
                lambda l: _normalize(l.product_id.name) == _normalize(concept['descripcion'])
            )
            if len(matching_lines) != 1:
                code_candidates = (product_lines - used_lines).filtered(
                    lambda l: (
                        l.product_id.default_code
                        and re.search(
                            r'\b' + re.escape(l.product_id.default_code) + r'\b',
                            concept['descripcion'], re.IGNORECASE,
                        )
                    )
                )
                if len(code_candidates) == 1:
                    matching_lines = code_candidates
                elif len(product_lines - used_lines) == 1:
                    matching_lines = product_lines - used_lines
                else:
                    matching_lines = self.env['account.move.line']
            if len(matching_lines) != 1:
                self._meli_flag_credit_note_needs_manual_review(credit_note_document, _(
                    "Mercado Libre generated a partial-refund credit "
                    "note (%(document)s) for '%(product)s', but it could "
                    "not be matched to exactly one product line on "
                    "invoice %(invoice)s — review manually."
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'product': concept['descripcion'],
                    'invoice': source_invoice.name,
                })
                return self.env['account.move']
            matching_line = matching_lines
            used_lines |= matching_line
            vals = matching_line.with_context(include_business_fields=True).copy_data()[0]
            vals.pop('move_id', None)
            vals['quantity'] = concept['cantidad']
            # Fix 2026-09-22 round 4 (same real case above): the CFDI's
            # own Importe for this concept is normally cantidad * the
            # line's own unit price (a plain N-of-M-units return), but
            # a genuine DISCOUNT-type partial refund — same quantity,
            # no units returned — reports LESS than that (here, exactly
            # 90% of it). Copying the line's own full price in that
            # case would over-credit the customer by the difference,
            # so the document's own amount is used directly as this
            # line's price instead — "the document IS the truth", the
            # same principle _meli_relate_partial_cancellation_credit_
            # note already applies for its own pack case.
            expected_importe = concept['cantidad'] * matching_line.price_unit
            is_discount_type = abs(expected_importe - concept['importe']) > 0.05
            if is_discount_type:
                vals['price_unit'] = concept['importe'] / concept['cantidad']
                # Fix 2026-09-22 round 6 (real regression round 5's own
                # fix introduced, caught by the test suite): sale_line_ids
                # is kept here — removing it also made this line invisible
                # to sale.order.invoice_ids (Odoo core computes that
                # purely from order_line.invoice_lines.move_id, the same
                # inverse relation), breaking source_invoice/existing_
                # refund lookups and the "Facturas" smart button for this
                # exact credit note. meli_discount_adjustment is the
                # actual, narrower fix: flags this line so SaleOrderLine.
                # _compute_qty_invoiced (see its own override) adds its
                # quantity back after core's own subtraction — a discount
                # never represents a returned unit, so it must never net
                # qty_invoiced down, but it still needs to stay linked to
                # the sale line for everything else. A genuine physical
                # return (this `if` branch not taken) is left completely
                # untouched — that case SHOULD net qty_invoiced down,
                # since the unit really did come back.
                vals['meli_discount_adjustment'] = True
            line_vals_list.append(vals)

        credit_note = self.env['account.move'].create({
            'move_type': 'out_refund',
            'reversed_entry_id': source_invoice.id,
            'partner_id': source_invoice.partner_id.id,
            'currency_id': source_invoice.currency_id.id,
            'company_id': source_invoice.company_id.id,
            'invoice_date': (
                self._meli_document_invoice_date(credit_note_document)
                or source_invoice.invoice_date
            ),
            'journal_id': source_invoice.journal_id.id,
            'invoice_origin': source_invoice.invoice_origin,
            'ref': _(
                "Mercado Libre partial-refund credit note %s"
            ) % (credit_note_document.meli_invoice_id or credit_note_document.id),
            'invoice_line_ids': [(0, 0, vals) for vals in line_vals_list],
        })
        credit_note.action_post()
        return credit_note

    def _meli_relate_partial_cancellation_credit_note(
        self, lines, cancelled_order_id, extra_payment_lines_by_document=None,
    ):
        """Builds (or finds, if a previous call already built it — this
        method is idempotent, same convention as everything else in this
        file) a credit note for the product line(s) a devolución
        document's own XML concepts actually name — within a
        consolidated pack invoice — matched across the WHOLE pack's
        invoice, not just cancelled_order_id's own sibling. Called only
        from _meli_process_partial_cancellation — see that method's own
        docstring for why the credit note is related BEFORE the stock
        return.

        `extra_payment_lines_by_document` (2026-09-21, real production
        case order S974870/pack 2000015124781237): an optional
        {meli.invoice.document id: account.move.line recordset} map —
        _meli_reconcile_invoicing's own Step 2 can already have broken
        a STALE credit note's own payment reconciliation itself (its
        live_partial_cancellation_credit_note gate, now a cancel-and-
        rebuild instead of a permanent block) before ever reaching this
        method — without this, that payment would be silently orphaned
        (never reconciled with anything again) once this method
        rebuilds a correct credit note for the exact same document.
        Merged into whatever this method's own internal stale-refund
        handling below already captures for that same document.

        Fix 2026-09-19 (user-directed, real production case confirmed
        against order 854722/pack 2000015098921997): Mercado Libre files
        ONE shared devolución document per pack, tagged under whichever
        sibling it treats as "primary" (cancelled_order_id here), but
        its own XML lists a separate cfdi:Concepto per product actually
        returned — for that real order, BOTH "LONA...NEGRO" (2 pzas,
        $326.28) and "LONA...VERDE" (2 pzas, $160.76) appeared in the
        SAME document, total $564.96. The previous version of this
        method matched only cancelled_order_id's own `lines` argument,
        so it built a credit note for $378.48 (NEGRO alone) — an amount
        that never appeared anywhere in Mercado Libre's own XML — and
        nothing ever revisited it once VERDE's own line existed. The
        `lines` argument is now used only as a last-resort fallback (see
        below); matching is otherwise done directly against the
        document's own concepts, exactly like
        _meli_build_partial_credit_note already does for the
        (deliberately paused) partial-refund case — the same technique,
        now applied here too, so "the document IS the truth" instead of
        "whichever sibling's order_id happens to be on the document".

        All-or-nothing (2026-09-19, user decision: "no quiero separar
        las dos notas de crédito... quiero que cada documento sea la
        verdad contra nuestra venta"): if even one of the document's own
        concepts can't be matched to exactly one invoiced product yet
        (most commonly a pack sibling whose own line hasn't been added),
        NOTHING is applied — no partial credit note gets created. This
        sets meli_needs_manual_mismatch_review ("pendiente por descuadre
        venta y factura") and is naturally retried later: both
        _meli_add_pack_sibling_lines's own callers (once the missing
        sibling's line gets added) and the ordinary reconciliation
        re-entry points call this again, so once every concept resolves
        the whole credit note is created in one shot, matching Mercado
        Libre's own document exactly.

        Self-correcting for an ALREADY-applied, now-known-incomplete
        credit note (the exact real bug above): if a live out_refund is
        already related to this credit_note_document but doesn't cover
        every line the document's own concepts now resolve to, that
        stale refund is cancelled — via
        _meli_cancel_account_move_breaking_reconciliation, which breaks
        payment reconciliation if it has to — and replaced with a fresh
        one covering everything, re-reconciled against the same
        payment(s) it had. Never leaves two live credit notes for the
        same document, and never leaves one permanently short.

        Returns the related account.move (out_refund) — either just
        created/corrected, or the one already correctly related from a
        previous, idempotent call — or an empty 'account.move'
        recordset when there's nothing to relate yet.
        """
        self.ensure_one()
        # Local import: see _meli_reconcile_invoicing's own docstring for
        # why this can't be a module-level import (circular import with
        # meli_invoice_document.py).
        from .meli_invoice_document import (
            MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES, MELI_INVOICE_DEAD_STATUSES,
            MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES,
        )
        meli_invoice_not_usable_statuses = (
            MELI_INVOICE_DEAD_STATUSES | MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES
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
            ('status', 'not in', list(meli_invoice_not_usable_statuses)),
        ], order='create_date asc, id asc')
        if not credit_note_documents:
            return self.env['account.move']

        source_invoice = self.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted'
        )[:1]
        credit_note_created = self.env['account.move']

        def _normalize(text):
            return ' '.join((text or '').split()).casefold()

        for credit_note_document in credit_note_documents:
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

            # Fix 2026-09-21 round 2 (user-directed, real gap this same
            # segment's cancel-and-rebuild fix exposed): matching used
            # to be scoped to source_invoice's OWN invoice_line_ids —
            # but a sibling that's already fully returned/credited has
            # nothing left to invoice, so it never appears on whichever
            # invoice is CURRENTLY posted (rebuilt fresh after a
            # refacturación). That made an already-correct credit note
            # impossible to rebuild once its own invoice got cancelled
            # for an unrelated reason (e.g. a DIFFERENT sibling's own
            # refacturación) — "review manually" with no automatic way
            # forward. self.order_line never disappears (quantity is
            # never touched by this automation, confirmed throughout
            # this file) — matching against it instead means a credit
            # note can always be rebuilt from the sale's own truth,
            # independent of whichever invoice happens to exist right
            # now.
            product_lines = self.order_line.filtered(
                lambda l: l.product_id and not l.display_type
            )
            xml_bytes = base64.b64decode(credit_note_document.xml_file)
            concepts = credit_note_document._meli_parse_concepts_from_xml(xml_bytes)

            matched_lines = self.env['sale.order.line']
            matched_concept_by_line_id = {}
            unmatched_concepts = []
            if concepts:
                used_lines = self.env['sale.order.line']
                for concept in concepts:
                    # Fix 2026-09-22 round 4 (user-directed, real case:
                    # order S955446/2000018285646206, document
                    # 3000000093287606, concept "Pack 10 Sillas
                    # Plegables Reforzadas Plástico Negro" for catalog
                    # product "[SIPL01] JUEGO DE 10 SILLAS PLEGABLES")
                    # — same three extra layers _meli_correct_move_
                    # from_document already uses when Mercado Libre's
                    # own marketplace listing title shares no words
                    # with this catalog's internal product name: (1)
                    # exact name (as before), (2) the product's own
                    # default_code/SKU appearing as a whole word in the
                    # concept's own description, (3) exactly one
                    # product line remaining unclaimed by an earlier
                    # concept this same run — an unambiguous match
                    # regardless of text when there is nothing else it
                    # could be, (4) — new here, fix 2026-09-22 round 8,
                    # real case order S978385/pack 2000015158767125,
                    # devolución document with 2 concepts (CJC01 +
                    # JCP03) BOTH using Mercado Libre's own marketplace
                    # listing titles, neither matching by name/SKU, and
                    # with 2 lines genuinely still unclaimed at once
                    # (layer 3 can't disambiguate when more than one
                    # candidate remains) — quantity + pre-tax amount
                    # matching exactly, same $0.05 tolerance and
                    # technique _meli_correct_move_from_document's own
                    # equivalent fallback already uses, among the lines
                    # not already claimed by an earlier concept this
                    # same run.
                    candidates = product_lines.filtered(
                        lambda l: _normalize(l.product_id.name) == _normalize(concept['descripcion'])
                    )
                    if len(candidates) != 1:
                        code_candidates = (product_lines - used_lines).filtered(
                            lambda l: (
                                l.product_id.default_code
                                and re.search(
                                    r'\b' + re.escape(l.product_id.default_code) + r'\b',
                                    concept['descripcion'], re.IGNORECASE,
                                )
                            )
                        )
                        if len(code_candidates) == 1:
                            candidates = code_candidates
                        elif len(product_lines - used_lines) == 1:
                            candidates = product_lines - used_lines
                        else:
                            price_candidates = (product_lines - used_lines).filtered(
                                lambda l: (
                                    abs(l.product_uom_qty - concept['cantidad']) < 0.001
                                    and abs(l.price_unit * l.product_uom_qty - concept['importe']) <= 0.05
                                )
                            )
                            candidates = (
                                price_candidates if len(price_candidates) == 1
                                else self.env['sale.order.line']
                            )
                    if len(candidates) != 1:
                        unmatched_concepts.append(concept)
                    else:
                        used_lines |= candidates
                        matched_lines |= candidates
                        matched_concept_by_line_id[candidates.id] = concept
            else:
                # Fallback (no readable concepts on this XML): the old,
                # pre-2026-09-19 behaviour — scope to cancelled_order_id's
                # own lines argument, same as ever. `lines` is itself a
                # sale.order.line recordset (see _meli_sibling_lines),
                # so a plain intersection replaces the old sale_line_ids
                # bridge now that product_lines are sale lines too.
                matched_lines = product_lines & lines

            if unmatched_concepts:
                if not credit_note_document.meli_needs_manual_mismatch_review:
                    credit_note_document.meli_needs_manual_mismatch_review = True
                    self.message_post(body=_(
                        "Mercado Libre generated a credit note "
                        "(%(document)s, total $%(total)s) referencing order "
                        "%(order_id)s, but %(count)s of its own line "
                        "item(s) could not be matched to exactly one "
                        "invoiced product yet (most likely a pack sibling "
                        "still missing) — pendiente por descuadre venta y "
                        "factura. It will be applied automatically, in "
                        "full, once the missing product is added and the "
                        "totals agree."
                    ) % {
                        'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                        'total': '%.2f' % credit_note_document.meli_xml_total,
                        'order_id': cancelled_order_id,
                        'count': len(unmatched_concepts),
                    })
                continue

            if not matched_lines:
                self.message_post(body=_(
                    "Mercado Libre generated a credit note (%(document)s) for "
                    "order %(order_id)s (one individual order within this "
                    "pack), but its product line(s) could not be found on "
                    "this sale — review manually."
                ) % {
                    'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                    'order_id': cancelled_order_id,
                })
                continue

            existing_refund = self.invoice_ids.filtered(
                lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
                and m.meli_invoice_document_id == credit_note_document
            )[:1]
            payment_lines = self.env['account.move.line']
            if existing_refund:
                # Fix 2026-09-22 round 5: coverage is now checked by
                # product_id, not sale_line_ids — a DISCOUNT-type line
                # (see the price-building loop below) deliberately
                # omits sale_line_ids (so it doesn't net against
                # sale.order.line.qty_invoiced, which would risk
                # Odoo re-invoicing a product that was never actually
                # returned), so a sale_line_ids-based check would never
                # recognize it as covered and would keep needlessly
                # cancelling and rebuilding this same, already-correct
                # credit note on every idempotent re-entry. product_id
                # is set on every invoice line regardless, and
                # matched_lines is always resolved to a specific
                # product per concept — just as reliable a coverage
                # signal here.
                wanted_product_ids = set(matched_lines.product_id.ids)
                covered_product_ids = set(
                    existing_refund.invoice_line_ids.filtered(
                        lambda l: l.display_type == 'product'
                    ).mapped('product_id.id')
                )
                if wanted_product_ids <= covered_product_ids:
                    # Already fully, correctly related — most likely a
                    # re-entry of this same, already-correct call.
                    credit_note_created = credit_note_created or existing_refund
                    continue
                # Stale/incomplete (the real 854722-shaped bug): cancel
                # and rebuild covering everything the document reports,
                # breaking and then restoring payment reconciliation if
                # needed — never left half-applied, never a second
                # document for the same credit-note document.
                payment_lines = self._meli_cancel_account_move_breaking_reconciliation(
                    existing_refund,
                )
            else:
                # Critical fix (kept from before 2026-09-19): a DIFFERENT
                # credit-note document must never create a second live
                # out_refund over lines some other document already
                # covers — a genuine REPLACEMENT devolución CFDI
                # (confirmed possible by _meli_upsert's own docstring)
                # needs accounting judgment out of scope for this method.
                # Fix 2026-09-22 round 5: matched by product_id, not
                # sale_line_ids — see the identical fix a few lines up
                # for why a discount-type line can't be relied on to
                # carry sale_line_ids at all.
                matched_product_ids = set(matched_lines.product_id.ids)
                other_live_refund_lines = self.invoice_ids.filtered(
                    lambda m: m.move_type == 'out_refund' and m.state != 'cancel'
                    and m.meli_invoice_document_id != credit_note_document
                ).invoice_line_ids.filtered(
                    lambda l: l.display_type == 'product'
                    and l.product_id.id in matched_product_ids
                )
                if other_live_refund_lines:
                    self.message_post(body=_(
                        "Mercado Libre generated another credit note "
                        "(%(document)s) for order %(order_id)s (one "
                        "individual order within this pack), but a credit "
                        "note already covers some of its own line(s) — "
                        "review manually, this new document was not "
                        "applied automatically."
                    ) % {
                        'document': credit_note_document.meli_invoice_id or credit_note_document.id,
                        'order_id': cancelled_order_id,
                    })
                    continue

            # Fix 2026-09-21 round 2: built from each matched SALE
            # line's own _prepare_invoice_line() (the same standard
            # Odoo method _create_invoices() itself uses) now that
            # matched_lines are sale.order.line records, not invoice
            # lines copied from whichever invoice happened to be
            # posted — see the comment on product_lines above. quantity
            # defaults to qty_to_invoice (usually 0/negative here, since
            # this product has nothing left TO invoice — that's the
            # whole point), so it's overridden to the sale line's own
            # product_uom_qty: the same quantity copy_data() used to
            # carry over from the old invoice line, since "quantity is
            # never touched by this automation" makes it the correct,
            # stable stand-in for "how much of this was actually
            # invoiced and is now being credited".
            line_vals_list = []
            for matched_line in matched_lines:
                vals = matched_line._prepare_invoice_line()
                concept = matched_concept_by_line_id.get(matched_line.id)
                if concept:
                    # Fix 2026-09-24 (real production bug, order
                    # S975652/pack 2000015132783453, user-directed): a
                    # genuine partial-QUANTITY refund (here: 1 of the 2
                    # units ordered) used to fall into the `else` branch
                    # below — abs(concept['importe'] - concept['cantidad']
                    # * matched_line.price_unit) reads well under the
                    # 5-cent tolerance for this exact case (the concept's
                    # own amount already IS cantidad × the normal unit
                    # price, just for the REDUCED cantidad, not the
                    # line's full product_uom_qty) — so this used to
                    # silently credit the line's FULL ordered quantity
                    # (2 units, the whole order's own total) for what
                    # Mercado Libre's own document plainly reports as a
                    # refund of only 1. The document's own concept is
                    # always the ground truth for how much to credit —
                    # never the sale line's own ordered quantity, which
                    # this automation never touches (see this method's
                    # own docstring) and so has no idea how many units
                    # were actually returned.
                    vals['quantity'] = concept['cantidad']
                    if abs(
                        concept['importe'] - concept['cantidad'] * matched_line.price_unit
                    ) > 0.05:
                        # Fix 2026-09-22 round 4 (user-directed, real
                        # case above): a genuine DISCOUNT-type partial
                        # refund — same quantity as the sale, no units
                        # returned, but Mercado Libre's own CFDI amount
                        # is LESS than cantidad × this line's normal
                        # unit price (here, exactly 90% of it). Copying
                        # the sale line's own normal price would
                        # over-credit the customer by the difference.
                        # The document's own amount is used directly as
                        # this line's price instead — "the document IS
                        # the truth", the same principle this method
                        # already applies to WHICH products/quantities
                        # to credit, now extended to HOW MUCH.
                        vals['price_unit'] = concept['importe'] / concept['cantidad']
                        # Fix 2026-09-22 round 6 (real regression round 5's
                        # own fix introduced, caught by the test suite) — see
                        # _meli_build_partial_credit_note's own identical fix
                        # for the full explanation: sale_line_ids is kept
                        # (never popped) here — removing it also made this
                        # line invisible to sale.order.invoice_ids itself
                        # (Odoo core computes that purely from order_line.
                        # invoice_lines.move_id, the same inverse relation),
                        # breaking source_invoice/existing_refund lookups and
                        # the "Facturas" smart button for this credit note.
                        # meli_discount_adjustment flags this line instead, so
                        # SaleOrderLine._compute_qty_invoiced's own override
                        # adds its quantity back after core's subtraction —
                        # never returned, so it must never net qty_invoiced
                        # down, but still needs to stay linked to the sale
                        # line for everything else.
                        vals['meli_discount_adjustment'] = True
                else:
                    # No readable concept for this line at all (the
                    # "Fallback (no readable concepts on this XML)"
                    # branch above) — the whole-line, full-quantity
                    # credit is the correct behavior ONLY in that
                    # genuinely concept-free case.
                    vals['quantity'] = matched_line.product_uom_qty
                line_vals_list.append(vals)

            credit_note = self.env['account.move'].create({
                'move_type': 'out_refund',
                'reversed_entry_id': source_invoice.id,
                'partner_id': source_invoice.partner_id.id,
                'currency_id': source_invoice.currency_id.id,
                'company_id': source_invoice.company_id.id,
                # Fix 4 (2026-09-09, user-directed follow-up): dated from
                # the real Mercado Libre document's own fiscal date, not
                # copied from source_invoice's own (unrelated) invoice
                # date — see _meli_document_invoice_date's own
                # docstring. Falls back to source_invoice's own date only
                # in the (never actually seen) case issue_date wasn't
                # parseable, so this never regresses to a blank date.
                'invoice_date': (
                    self._meli_document_invoice_date(credit_note_document)
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
            payment_lines |= (extra_payment_lines_by_document or {}).get(
                credit_note_document.id, self.env['account.move.line'],
            )
            self._meli_reconcile_move_with_payment_lines(credit_note, payment_lines)
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

    # Fix 2026-09-24: _meli_recover_aborted_transaction() used to live
    # here — a bare self.env.cr.rollback() (full transaction rollback)
    # called from inside the except blocks above whenever
    # _meli_reconcile_invoicing() failed. Removed entirely: every one of
    # its call sites now wraps the risky call in its own `with
    # self.env.cr.savepoint():` instead, which performs the equivalent
    # recovery (a clean, postable transaction, ready for the chatter
    # message that follows) via a properly SCOPED rollback (ROLLBACK TO
    # SAVEPOINT) rather than a full one. The full rollback this method
    # did was nested inside queue_job_cron_jobrunner's own outer per-job
    # savepoint (this whole call chain runs inside a queue.job) and
    # silently invalidated it — confirmed in production (orders
    # 993840/994428) via the resulting InvalidSavepointSpecification /
    # InFailedSqlTransaction cascade, which left the job stuck 'pending'
    # forever and blocked every other job queued behind it.

    @api.model
    def _meli_create_from_order_data(self, config, order_data):
        order_id = str(order_data.get('id'))
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

        # Fix 2026-09-24 (real production bug, packs 2000015175016735/
        # 2000015180304759, and — round 2 — plain non-pack orders
        # 993840/994428/994473, all user-directed): two queue.job
        # workers running genuinely concurrently (confirmed: this
        # started right after a second "Queue Job Runner" cron was
        # added) can both reach the "does a sale.order already exist
        # for this?" check below before either one's own create() has
        # committed — both see nothing, both create their OWN separate
        # sale.order for the same real Mercado Libre order/pack. Round 1
        # only locked by pack_id, on the theory that a plain, non-pack
        # order was already fully covered by the `existing` search
        # further below — wrong: that search is read-only and racy just
        # like the pack one, and round 2's real production evidence
        # (three separate 'cancelled on arrival' orders, each crashing
        # with MissingError on a sale.order its OWN job had just
        # created and lost to a concurrent duplicate) confirmed the
        # exact same race happens for a lone order too. Locked by
        # `pack_id or order_id` now, and moved BEFORE the `existing`
        # search below (not just before the pack-sibling branch), so
        # the entire "does this already exist? if not, create it" check
        # is atomic for BOTH shapes — a Postgres transaction-scoped
        # advisory lock: the second worker blocks here until the first
        # one's transaction (its own queue.job commit, per
        # queue_job_cron_jobrunner's own commit=True) is done, then sees
        # the first worker's sale.order for real instead of racing past
        # it. hashtext() collapses the id string to the int Postgres'
        # single-argument pg_advisory_xact_lock overload expects; a hash
        # collision between two DIFFERENT ids would only ever cost
        # unnecessary serialization between them, never incorrect data.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (pack_id or order_id,),
        )

        existing = self.search([('meli_order_id', '=', order_id)], limit=1)
        if existing:
            return existing

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
            '!', '&',
            ('create_uid', '=', MELI_ADOPTION_EXCLUDED_CREATE_UID),
            ('partner_id', '=', MELI_ADOPTION_EXCLUDED_PARTNER_ID),
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
            ) % adoption_ref, notify_failure_team=False)
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
        elif not adoption_candidates:
            # Fix 2026-09-22 round 2 (user-directed correction — real
            # production bug, confirmed live: order/pack
            # 2000018567538874, S975817 vs. the automatically created
            # duplicate S977578; user's own words: "equis quien la
            # creo, si el reference ya existe igualito no lo crees"):
            # NOT scoped to the Horacio/Mercado-Libre exclusion alone
            # (round 1's narrower fix) — ANY OTHER, non-connector
            # sale.order that already carries this exact reference/
            # client_order_ref, for WHATEVER reason it didn't qualify
            # as a clean adoption_candidates match above (the Horacio/
            # Mercado-Libre exclusion, already cancelled, some other
            # data oddity), must block this method from falling
            # straight through to creating a real, separate duplicate
            # sale here — its own stock reservation included. The
            # `existing` search at the very top of this same method
            # already short-circuited on an exact meli_order_id match;
            # this is only ever reached for a genuinely different
            # case: same reference, not yet linked by meli_order_id at
            # all.
            #
            # Fix round 3 (2026-09-22, real regression THIS round 2 fix
            # itself introduced — order 2000018592316830/pack
            # 2000015158767125, S978385 stuck missing a sibling line
            # for hours despite _meli_import_order retrying it several
            # times with no error): ('meli_sync_source', '=', False)
            # restored here — without it, this branch also matched an
            # ALREADY-connector-managed pack sale (meli_sync_source
            # already set), silently blocking the completely different,
            # legitimate "known pack, add this sibling's own line"
            # handling a few dozen lines below (`if order_data.get(
            # 'status') != 'paid' and pack_id: pack_order = self.
            # sudo().search([('meli_pack_id', '=', pack_id)], ...)`)
            # from ever being reached at all. That handling is exactly
            # what a pack's own late/cancelled sibling needs — this
            # duplicate guard must only ever catch a genuinely FOREIGN
            # order (never touched by this connector), same scope
            # adoption_candidates itself already uses above.
            any_existing_match = self.search([
                '|', ('reference', '=', adoption_ref), ('client_order_ref', '=', adoption_ref),
                ('meli_sync_source', '=', False),
            ], limit=1)
            if any_existing_match:
                any_existing_match._meli_notify_queue_job_managers(_(
                    "Mercado Libre order/pack %(ref)s arrived, matching "
                    "this sale's own reference/OC Cliente — but this "
                    "sale could not be automatically adopted/linked "
                    "(review its own state/creator/sync status). No "
                    "new sale was created either, to avoid a duplicate "
                    "— review manually whether this sale IS that real "
                    "Mercado Libre order (link it by hand) or is a "
                    "genuinely separate, coincidental match."
                ) % {'ref': adoption_ref})
                return self.browse()

        if order_data.get('status') != 'paid' and pack_id:
            pack_order = self.sudo().search([('meli_pack_id', '=', pack_id)], limit=1)
            if pack_order:
                # Fix 2026-09-18 (real production bug — see
                # _meli_ensure_all_pack_siblings_imported's own
                # docstring): a sibling discovered for the FIRST time
                # already in a non-'paid' state (the common case: it's
                # usually already 'cancelled' on Mercado Libre's own
                # side, which is exactly why a credit-note document for
                # it showed up at all) has no line of its own yet on
                # this pack. Falling straight through to
                # _meli_flag_status_change below — this branch's
                # original, only-ever-tested shape — leaves
                # _meli_apply_partial_cancellation with NOTHING to act
                # on (_meli_sibling_lines finds no line, and its own
                # fallback only ever covers THIS order's first-seen
                # sibling, never a different one): no credit note
                # relation, no stock return, just a misleading "had no
                # delivered stock" chatter note for a sibling that very
                # much was delivered. Adding the line first — same
                # "recovery" shape _meli_recover_cancelled_on_arrival_
                # full already uses for a single order's own cancelled-
                # on-arrival case — gives the reconciliation that
                # follows real data to work with. Skipped for an
                # ADOPTED pack (meli_adopted=True): _meli_add_pack_
                # sibling_lines already refuses to run for one anyway
                # (see its own caller a few lines below), matching this
                # module's standing rule that adoption never touches an
                # order's own commercial details.
                #
                # Fix 2026-09-18 (user-directed follow-up): NOT scoped to
                # Full orders only, unlike the automatic-cancellation
                # side of a sibling's status change further down
                # (_meli_flag_status_change, called unconditionally right
                # below) — that automation stays paused for non-Full
                # packs (spec section 5), confirmed by this exact test
                # shape already existing before this fix
                # (test_pack_sibling_status_change_notification_flags_
                # manual_review). Adding the sibling's own line, though,
                # is pure traceability — it neither confirms, cancels,
                # nor moves stock for a non-Full order (see
                # _meli_add_pack_sibling_lines's own docstring: its only
                # stock/picking side effect is gated behind 'MLF' in
                # self.origin, i.e. Full only) — so it's safe, and
                # desired, for every pack regardless of fulfillment type.
                if not pack_order.meli_adopted:
                    existing_line = self.env['sale.order.line'].sudo().search(
                        [('meli_order_id', '=', order_id), ('order_id', '=', pack_order.id)],
                        limit=1,
                    )
                    if not existing_line:
                        pack_order._meli_add_pack_sibling_lines(order_data, order_id)
                        # Fix 2026-09-19 (user-directed): a sibling
                        # discovered so late that its own line only gets
                        # added AFTER this whole Full pack was already
                        # cancelled has no way to ever get delivered
                        # through the ordinary path — see
                        # _meli_force_deliver_cancelled_sibling_line's
                        # own docstring for why, and why it's safe/
                        # correct to do retroactively.
                        is_full = bool(
                            config.warehouse_fulfillment_id
                            and pack_order.warehouse_id == config.warehouse_fulfillment_id
                        )
                        if is_full and pack_order.state == 'cancel':
                            pack_order._meli_force_deliver_cancelled_sibling_line(order_id)
                        # Fix 2026-09-19 (user-directed, real production
                        # case): a sibling arriving late is exactly what
                        # unblocks an EXISTING invoice/credit-note that
                        # was previously stuck "pendiente por descuadre
                        # venta y factura" (meli_needs_manual_mismatch_
                        # review) for lack of this exact product line —
                        # nothing else re-checks that on its own once the
                        # line is added, so it's triggered explicitly
                        # here rather than relying on some later,
                        # unrelated event to happen to re-enter
                        # reconciliation. Also what makes the bulk
                        # historical-repair action (action_meli_repair_
                        # all_historical_packs) self-correct each
                        # already-wrong invoice/credit note it touches,
                        # not just add the missing line.
                        pack_order._meli_reconcile_invoicing()
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
        #
        # Fix 2026-09-14: a plain 404 from /orders/{id}/shipments is no
        # longer treated as a fetch failure at all — _meli_fetch_shipment_
        # records itself now falls back to the documented per-shipment
        # endpoint when that happens (see its own docstring: confirmed in
        # production, order 2000018458354168, that the shipment genuinely
        # existed — Full/fulfillment — even though this specific endpoint
        # 404'd on it). Only a REAL transient failure (5xx, timeout,
        # connection error — including a 404 on the fallback endpoint
        # too, meaning even that couldn't resolve it) still reaches here
        # as a RequestException and gets retried.
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
        # 2026-09-20 user request: a visible, filterable record of which
        # shipping scheme Mercado Libre itself reports for this order —
        # confirmed live against real orders: 'fulfillment' in
        # logistic_type means Full; otherwise the shipment's own mode is
        # either 'custom' (seller manages the courier directly) or
        # 'me2' (ordinary Mercado Envíos — "tradicional"). No shipment
        # at all (shipment_records empty) leaves this False.
        shipment_mode = next(
            (s.get('mode') for s in shipment_records or [] if s.get('type') == 'forward'),
            None,
        )
        meli_shipping_scheme = (
            'full' if is_fulfillment
            else 'custom' if shipment_mode == 'custom'
            else 'traditional' if shipment_mode == 'me2'
            else False
        )
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
                added_to = pack_order._meli_add_pack_sibling_lines(order_data, order_id)
                # Fix 2026-09-19 (user-directed): same trigger as the
                # already-cancelled-pack branch above — see that call's
                # own comment for why this needs to happen explicitly
                # right here, not left to some other event.
                #
                # Fix 2026-09-23 (real production bug, pack
                # 2000015116272807/S847810): left completely unguarded
                # before this fix — when pack_order is already 'cancel'
                # (its own first-seen sibling arrived already cancelled
                # and went through the full recovery sequence before
                # this LATER sibling was even known), _create_invoices()
                # inside _meli_reconcile_invoicing() raises UserError
                # ("no hay artículos disponibles para facturar") since a
                # cancelled sale has nothing invoiceable. Uncaught here,
                # that exception aborted the ENTIRE call — including the
                # sibling line _meli_add_pack_sibling_lines had JUST
                # added moments above — so the sibling's own line was
                # silently lost on every single retry (all 8, identical
                # failure each time), never surfacing anywhere for a
                # human to see. Same savepoint + broad except convention
                # stock_picking.py's own _action_done already uses for
                # this exact same call (a failure here is an invoicing/
                # CFDI-side hiccup, never a reason to lose commercial
                # data — the sale itself, the line just added, and this
                # sibling's own eventual cancellation/stock-return are
                # all handled elsewhere and must survive regardless).
                try:
                    with self.env.cr.savepoint():
                        pack_order._meli_reconcile_invoicing()
                except Exception:
                    _logger.exception(
                        "Mercado Libre order %s: invoicing reconciliation "
                        "failed after adding individual order %s's own "
                        "line to this pack — left pending for manual "
                        "review (the line itself was kept).",
                        pack_order.client_order_ref, order_id,
                    )
                return added_to

        resolved_lines, unmapped_skus = self._meli_build_order_lines(order_data, order_id)

        # Fix 2026-09-22 (real production case S979113/pack
        # 2000015160414211, user-directed): a genuine, immutable signal
        # for "this order is Mercado Libre's own resale/1P" — confirmed
        # live against two real orders: static_tags contains
        # 'meli_resale' for a resale order and never for an ordinary
        # one, unlike the ordinary, seller/system-editable `tags` array
        # or `context.flows`, both of which carried 'catalog' on BOTH
        # kinds of orders in that same real comparison and are already
        # known unreliable (see _meli_fetch_buyer_shipping_surcharge's
        # own docstring for the earlier regression that first proved
        # this). Known at order-creation time, straight from the order
        # resource itself — no need to wait for its own factura
        # document at all, unlike meli_transaction_type (only known
        # once that document exists). Mercado Libre's own resale
        # markup/logistics charge to the buyer never belongs on this
        # sale (see _meli_repair_wrong_shipping_line_now's own
        # docstring, which still removes it after the fact for any
        # order that predates this check, or any static_tags edge case
        # this doesn't catch) — so the buyer-shipping-surcharge line is
        # simply never added below in the first place, for either
        # shipping scheme.
        is_known_resale = 'meli_resale' in (order_data.get('static_tags') or [])

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
            if not is_known_resale:
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
        else:
            # Fix 2026-09-19/20 (user-directed, real production case
            # order 856288/S844547): the common, non-custom shipment
            # case ('me2', every ordinary Full order included) — see
            # _meli_fetch_buyer_shipping_surcharge's own docstring for
            # why this was missing entirely before. Reuses the exact
            # same config.shipping_item_id product the custom-shipping
            # line above uses (2026-09-19 user decision: "shipping-va
            # igual asignar en el cargo") — one single shipping-surcharge
            # product either way.
            if not is_known_resale:
                try:
                    buyer_shipping_cost = self._meli_fetch_buyer_shipping_surcharge(
                        config, order_id, order_data, shipments=shipment_records,
                    )
                except requests.exceptions.RequestException as err:
                    raise RetryableJobError(
                        f"Could not fetch Mercado Libre shipment costs for "
                        f"order {order_id} — will retry.", seconds=30,
                    ) from err
                if buyer_shipping_cost:
                    if not config.shipping_item_id:
                        raise UserError(_(
                            "Configure 'Shipping Item' on the Mercado Libre "
                            "connection (Mercado Libre > Settings) before "
                            "importing this order (Mercado Libre order %s) "
                            "— Mercado Libre charged the buyer for shipping "
                            "and this connector needs a product to reflect "
                            "that on the sale."
                        ) % order_id)
                    shipping_price_unit = self._meli_price_unit_untaxed(
                        config.shipping_item_id, buyer_shipping_cost,
                    )
                    resolved_lines.append((
                        (0, 0, {
                            'product_id': config.shipping_item_id.id,
                            'product_uom_qty': 1,
                            'price_unit': shipping_price_unit,
                        }),
                        {
                            'sku': _('Mercado Envíos shipping'),
                            'product_id': config.shipping_item_id.id,
                            'ml_unit_price': buyer_shipping_cost,
                            'price_unit': shipping_price_unit,
                        },
                    ))
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

        # date_closed (when Mercado Libre closed the order, which
        # coincides with payment confirmation) rather than date_created
        # — orders are only ever imported once status='paid', so what
        # matters for sales reporting is when the money was secured, not
        # when the cart was started. With deferred payment methods
        # (OXXO, transfer) date_created can be days earlier and would
        # misdate the sale into the wrong accounting period. Decided
        # with the user 2026-09-08.
        meli_date_order = (
            self._meli_parse_datetime(order_data.get('date_closed'))
            or fields.Datetime.now()
        )

        vals = {
            'company_id': config.company_id.id,
            'partner_id': config.partner_id.id,
            'partner_shipping_id': shipping_partner_id,
            'partner_invoice_id': config.partner_id.id,
            'meli_delivery_contact_status': delivery_contact_status,
            'meli_shipping_id': meli_shipping_id,
            'meli_buyer_id': meli_buyer_id,
            'meli_shipping_scheme': meli_shipping_scheme,
            'client_order_ref': customer_ref,
            'reference': customer_ref,
            'origin': f'XE-{tag}-XEBRANDS',
            'meli_sync_source': 'xe_meli_connector',
            'meli_auto_recovered': True,
            'meli_order_id': order_id,
            'meli_pack_id': pack_id,
            'meli_last_status': order_data.get('status'),
            'meli_order_date_created': self._meli_parse_datetime(order_data.get('date_created')),
            'meli_order_date_closed': self._meli_parse_datetime(order_data.get('date_closed')),
            'date_order': meli_date_order,
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
            order._meli_notify_price_fallbacks(price_debug)
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
            except OperationalError as err:
                # Fix 2026-09-14 (real production incident, order
                # S841238): "no se pudo serializar el acceso debido a un
                # update concurrente" is Postgres refusing to commit a
                # transaction that raced another one over the same
                # rows — genuinely transient (same class of error
                # queue_job_cron_jobrunner's own _process() already
                # retries for job processing in general), NOT a real
                # data/validation problem. Treating it the same as a
                # permanent failure below used to leave the order stuck
                # in draft forever, with no product ever delivered and
                # no invoice ever auto-created, since nothing else
                # revisits a plain draft order. Re-raising as
                # RetryableJobError lets the whole job (this order's
                # own creation) retry from scratch — safe: the next
                # attempt just doesn't find it in `existing` yet (this
                # transaction's create() rolls back along with it) and
                # creates+confirms it again.
                if err.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                    raise RetryableJobError(
                        f"Mercado Libre order {order.client_order_ref}: "
                        f"action_confirm() hit a transient database "
                        f"conflict — will retry.", seconds=15,
                    ) from err
                _logger.exception(
                    "Mercado Libre order %s: action_confirm() failed — "
                    "left in draft for manual review.",
                    order.client_order_ref,
                )
                order._meli_post_with_mention(_(
                    "This sale could not be confirmed automatically: "
                    "%s. Please review and confirm it manually, or use "
                    "the \"Retry Confirmation\" button on its Mercado "
                    "Libre invoice document."
                ) % str(err))
            except Exception as err:
                _logger.exception(
                    "Mercado Libre order %s: action_confirm() failed — "
                    "left in draft for manual review.",
                    order.client_order_ref,
                )
                order._meli_post_with_mention(_(
                    "This sale could not be confirmed automatically: "
                    "%s. Please review and confirm it manually, or use "
                    "the \"Retry Confirmation\" button on its Mercado "
                    "Libre invoice document."
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
            # Fix 2026-09-18 (real production bug, confirmed against
            # this exact database): core Odoo's own action_confirm()
            # (sale.order._prepare_confirmation_values()) unconditionally
            # overwrites date_order with fields.Datetime.now() on every
            # confirmation — silently clobbering the real payment date
            # this method deliberately set above (Decided with the user
            # 2026-09-08: date_order must reflect when Mercado Libre
            # closed the order, never the import/confirm time). Restored
            # here rather than skipped in the create() vals above: this
            # covers BOTH this method's own successful action_confirm()
            # a few lines up AND the case where the order was ALREADY
            # 'sale' the moment create() returned (some other automation
            # confirmed it first) — either way, whatever ran
            # action_confirm() already clobbered it by the time we're
            # here.
            order.date_order = meli_date_order
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
            # Fix 2026-09-16 (real production risk, confirmed against
            # this company's own real config: virtually every user,
            # including the ones automated flows run as, carries
            # sale.group_auto_done_setting — see sale.order.
            # action_confirm()'s own is_confirmation_locked check, keyed
            # off create_uid, not the acting user — so a pack's FIRST
            # sibling almost always leaves this order 'locked' the
            # moment it's confirmed). _meli_force_line_prices below
            # writes price_unit on the brand new line it just created —
            # a protected field on a locked order (core sale_order_line.
            # write()'s own "forbidden to modify... in a locked order"
            # guard, which checks the order's own locked flag
            # unconditionally, with no bypass for this or any other
            # automation). Same unlock-before/relock-after pattern
            # _meli_process_full_cancellation already uses for its own
            # action_cancel() call — a plain field write here, not
            # action_unlock()/action_lock(), deliberately: those buttons
            # (xe_pacific's own action_unlock() override in particular)
            # carry extra validation (e.g. rejecting a picking still
            # 'in transit') that has nothing to do with briefly relaxing
            # the lock just long enough to add one sibling's line.
            was_locked = self.locked
            if was_locked:
                self.locked = False
            self.write({'order_line': line_vals})
            # 2026-09-19 user request: this order already existed before
            # this product arrived — flag it so it can be found and
            # double-checked later, matching exactly what this method
            # does (add a sibling's line to an existing sale), for both
            # new siblings arriving live and the historical bulk repair
            # (action_meli_repair_all_historical_packs), which reaches
            # here the same way.
            self.meli_pack_had_sibling_added = True
            new_product_ids = {debug['product_id'] for _c, debug in resolved_lines}
            new_lines = self.order_line.filtered(
                lambda line: line.meli_order_id == order_id
                and line.product_id.id in new_product_ids
            )
            price_debug = [debug for _c, debug in resolved_lines]
            self._meli_force_line_prices(new_lines, price_debug)
            self._meli_notify_price_fallbacks(price_debug)
            if was_locked:
                self.locked = True
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
    def _meli_order_recovery_delay_seconds(self, order_data):
        """Seconds left in MELI_ORDER_RECOVERY_GRACE_MINUTES's window,
        counting from when this order was PAID — 0 once the window has
        already elapsed, or neither timestamp can be parsed (create it
        right now either way, no more waiting). See _meli_import_order's
        own use of this for why the window exists at all.

        date_closed, not date_created: same convention this method's own
        caller already uses for date_order (2026-09-08 decision) — with
        deferred payment methods (OXXO, transfer) date_created can be
        days before the order is actually paid, which would start (and
        immediately exhaust) Ventiapp's grace period long before Ventiapp
        could possibly have anything to inject yet. Falls back to
        date_created only if date_closed is somehow missing on a 'paid'
        order (shouldn't happen in practice).
        """
        paid_at = (
            self._meli_parse_datetime(order_data.get('date_closed'))
            or self._meli_parse_datetime(order_data.get('date_created'))
        )
        if not paid_at:
            return 0
        elapsed = fields.Datetime.now() - paid_at
        remaining = timedelta(minutes=MELI_ORDER_RECOVERY_GRACE_MINUTES) - elapsed
        return max(0, int(remaining.total_seconds()))

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

        Fix 2026-09-14 (real incident, order 2000018458354168): a 404
        here does NOT reliably mean "this order has no shipment" — this
        specific order's shipment was real and Full/fulfillment
        (confirmed live: /shipments/{shipping_id} returned it with
        logistic.type='fulfillment', substatus='pack_splitted'), yet
        this endpoint 404'd anyway, most likely because a pack-split
        moves the order/shipment association this resource depends on.
        Falls back to the documented single-shipment endpoint (see
        _meli_fallback_shipment_record) instead of silently discarding a
        real shipment's data.
        """
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return []
        try:
            shipments = config._api_get(
                f'/orders/{order_id}/shipments',
                headers={'X-New-Domain': 'true'},
            )
        except requests.exceptions.HTTPError as err:
            if err.response is None or err.response.status_code != 404:
                raise
            _logger.warning(
                "Mercado Libre order %s: /orders/%s/shipments 404'd — "
                "falling back to /shipments/%s directly.",
                order_id, order_id, shipping_id,
            )
            return self._meli_fallback_shipment_record(config, shipping_id)
        if isinstance(shipments, dict):
            shipments = [shipments]
        return shipments or []

    @api.model
    def _meli_fallback_shipment_record(self, config, shipping_id):
        """Fix 2026-09-14: used only when /orders/{id}/shipments 404's
        (see _meli_fetch_shipment_records) — reads the shipment directly
        by its own id via the documented GET /shipments/{id} (header
        x-format-new: true, confirmed against the official docs and
        against live data), and normalizes its nested `logistic` block
        into the SAME flat shape (type/mode/logistic_type/base_cost)
        _meli_fetch_logistic_type and _meli_fetch_custom_shipping_cost
        already parse from the primary endpoint — so neither of them
        needs to know a fallback ever happened.

        For a 'custom' shipment, also fetches /shipments/{id}/costs to
        recover the equivalent of the primary endpoint's own base_cost:
        confirmed live (order 2000018198314102) that /costs' gross_amount
        matches /orders/{id}/shipments' base_cost exactly (900 both).
        That second call only happens for a custom shipment that ALREADY
        needed the fallback — the rare case doubly rare — and degrades to
        0.0 (same as "not custom" from the caller's own perspective) if
        it fails, never blocking order creation over a surcharge.

        Returns [] if the shipment itself also 404's (genuinely gone) or
        isn't a 'forward' shipment — same "nothing to apply" contract
        the primary endpoint's own empty-list case already has.
        """
        try:
            shipment = config._api_get(
                f'/shipments/{shipping_id}', headers={'x-format-new': 'true'},
            )
        except requests.exceptions.HTTPError as err:
            if err.response is not None and err.response.status_code == 404:
                return []
            raise
        logistic = shipment.get('logistic') or {}
        if logistic.get('direction') != 'forward':
            return []
        record = {
            'type': 'forward',
            'mode': logistic.get('mode'),
            'logistic_type': logistic.get('type'),
        }
        if logistic.get('mode') == 'custom':
            try:
                costs = config._api_get(
                    f'/shipments/{shipping_id}/costs', headers={'x-format-new': 'true'},
                )
            except requests.exceptions.RequestException:
                _logger.warning(
                    "Mercado Libre shipment %s: custom shipping, but "
                    "/costs could not be fetched during the fallback — "
                    "freight surcharge left at 0.", shipping_id,
                )
            else:
                record['base_cost'] = (costs or {}).get('gross_amount') or 0.0
        return [record]

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
    def _meli_fetch_buyer_shipping_surcharge(self, config, order_id, order_data, shipments=None):
        """Fix 2026-09-19/20 (real gap found 2026-09-18, user-confirmed
        fix 2026-09-19): _meli_fetch_custom_shipping_cost only ever
        covers a shipment whose own mode is 'custom' (the rare case —
        XE manages the courier itself). The COMMON case — a normal
        Mercado Envíos shipment (mode='me2', including every ordinary
        Full/fulfillment order) — never had its own shipping cost
        fetched at all, so the buyer's own share of the freight never
        got its own sale.order line, even though Mercado Libre's real
        invoice/credit-note XML always includes it. That silent gap is
        exactly what made the sale's own amount_total permanently
        smaller than the real invoiced total for every such order (real
        production case: order 856288/S844547, sale $929.00 vs. real
        invoice $1,049.10) — this closes it.

        Confirmed live against the real API (meli.config id=112): GET
        /shipments/{id}/costs returns {"receiver": {"cost": X},
        "senders": [{"cost": Y}], ...} — `receiver.cost` is the BUYER's
        own contribution toward shipping (money Mercado Libre already
        included in what the customer was charged, and therefore in
        its own invoice total) — `senders[0].cost` is XE's own net
        freight expense, a completely different, internal cost/expense
        figure that has nothing to do with what the customer's own
        invoice says and must never be confused with this one. Only
        `receiver.cost` is ever returned here.

        Returns None (nothing to add) whenever: there's no shipment at
        all, the shipment IS 'custom' (already covered by
        _meli_fetch_custom_shipping_cost — must never double-charge
        the buyer for freight in two separate lines), the order is a
        catalog/resale one (see below), or the buyer's own cost share
        is zero/absent. A genuine fetch failure propagates as
        requests.exceptions.RequestException, same convention as
        _meli_fetch_custom_shipping_cost's own docstring explains —
        silently continuing without it would recreate the exact silent
        gap this fix closes.

        Fix 2026-09-21 (real production case caught before import,
        order 2000018568677372/pack 2000015136237497 — user-flagged
        from the seller-panel "REVENTA" tag): what Mercado Libre
        actually owes the SELLER for a resale/1P order is ONLY
        total_amount/paid_amount (here, $141.24 — matching the seller
        panel's own "Total a recibir"), which never includes the
        buyer's own shipping share ($110 in that same real order, from
        GET /shipments/{id}/costs' own receiver.cost) — that extra is
        Mercado Libre's own resale markup/logistics, money that never
        reaches XE and must never be added to this sale.

        Reverted 2026-09-22 (real production regression, 8 orders
        including S976474/S975750/S975093/S975074/S975038/S974980/
        S974975/S974973): this used to also skip whenever the order's
        own 'tags' included 'catalog', on the assumption that a
        catalog/buybox tag meant a resale order. Confirmed false live:
        a perfectly ordinary 'sale' (not resale) order can carry the
        'catalog' tag too (any listing using Mercado Libre's catalog
        ficha, regardless of who invoices it) — that guess was silently
        dropping a real, owed buyer-shipping charge (up to ~$190/order
        in these 8 cases) from orders that were never resale at all.
        There is no reliable order-resource signal for "is this
        resale" available at this point (before its own invoice
        document exists — see meli_transaction_type's own help text),
        so this is no longer guessed here at all: the line is now added
        for every order that has a real buyer shipping charge, resale
        included, and _meli_reconcile_invoicing's own end-of-call to
        _meli_repair_wrong_shipping_line_now removes it again the
        moment meli_transaction_type is actually confirmed 'resale' —
        see that method's own docstring.
        """
        shipping_id = (order_data.get('shipping') or {}).get('id')
        if not shipping_id:
            return None
        if shipments is None:
            shipments = self._meli_fetch_shipment_records(config, order_id, order_data)
        for shipment in shipments or []:
            if shipment.get('type') == 'forward' and shipment.get('mode') == 'custom':
                # Already handled by _meli_fetch_custom_shipping_cost —
                # never fetch/charge this a second time.
                return None
        costs = config._api_get(f'/shipments/{shipping_id}/costs')
        receiver_cost = (costs.get('receiver') or {}).get('cost') or 0.0
        return receiver_cost or None

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
    def _meli_order_payments_total(self, order_data):
        """Sum of this individual Mercado Libre order's own APPROVED
        payments' transaction_amount (what the buyer actually paid for
        the item(s), excluding shipping/taxes) — the price fallback
        _meli_build_order_lines uses when order_items[].unit_price
        comes back null.
        """
        payments = order_data.get('payments') or []
        return sum(
            p.get('transaction_amount') or 0.0
            for p in payments if p.get('status') == 'approved'
        )

    @api.model
    def _meli_build_order_lines(self, order_data, order_id):
        """Returns (resolved_lines, unmapped_skus). Each entry in
        resolved_lines is (create_command, debug_dict) — the debug_dict
        carries the raw Mercado Libre price alongside the computed
        price_unit, so callers can force the real price back onto the
        line after creation (see _meli_force_line_prices) and post the
        right chatter/manager notification for any price fallback used
        (see _meli_notify_price_fallbacks). Every line command stamps
        meli_order_id so it can always be traced back to the individual
        Mercado Libre order that added it, even when several individual
        orders share one sale.order (a pack).

        Fix 2026-09-18 (real production bug, order 2000018353085326):
        Mercado Libre's own 'meli_resale'/catalog orders report
        order_items[].unit_price as null (never a real 0) — confirmed
        by fetching this exact order from the live API: unit_price,
        total_amount and paid_amount were ALL null, yet
        payments[0].transaction_amount showed the real $162.80 the
        buyer paid. Before this fix, `unit_price or 0.0` silently
        turned that null into a genuine $0.0 line, which xe_pacific's
        own restrict_unit_price_zero() then used to reject the WHOLE
        order's creation outright.

        There is no way to recover a genuine per-SKU price when this
        happens — Mercado Libre gives no breakdown at all, only one
        total for the whole individual order — so every item in that
        order splits the same order's own payments total evenly by
        quantity (confirmed as the only viable option; user-approved
        2026-09-18). If payments themselves carry no usable amount
        either (no signal whatsoever), the line falls back to the
        minimum $0.01 instead of blocking the whole order, and the
        caller notifies this company's configured failure-notification
        users (see _meli_notify_price_fallbacks) — also user-approved,
        replacing the previous hard failure.
        """
        resolved_lines = []
        unmapped_skus = []
        order_items = order_data.get('order_items') or []
        total_quantity = sum(item.get('quantity') or 1 for item in order_items)
        payments_total = self._meli_order_payments_total(order_data)
        payments_unit_price = (
            payments_total / total_quantity
            if total_quantity and payments_total else None
        )
        for order_item in order_items:
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
            ml_unit_price = order_item.get('unit_price')
            used_payments_fallback = False
            used_minimum_price_fallback = False
            if ml_unit_price is None:
                # Only a genuinely missing (null) unit_price gets a
                # fallback — an explicit, real 0 from Mercado Libre is
                # left exactly as before (still ends up $0 and still
                # blocked by xe_pacific's own restrict_unit_price_zero()
                # below): that's a different, separate scenario this
                # fix was never meant to change.
                if payments_unit_price:
                    ml_unit_price = payments_unit_price
                    used_payments_fallback = True
                else:
                    used_minimum_price_fallback = True
            # Fix 2026-09-21 (user request, real order 2000018485873898):
            # order_items[].unit_price is ALREADY net of any coupon/
            # promotion discount — confirmed against Mercado Libre's own
            # docs ("unit_price: Precio unitario del ítem después de
            # aplicar los descuentos") and against this exact order's own
            # numbers (Ventiapp shows a struck-through "before" price of
            # $651.86 next to the real $618.00 charged, and
            # $651.86 - $618.00 = $33.86/unit × 2 units = $67.72 — exactly
            # Ventiapp's own "Descuento de cupón" line). So price_unit is
            # ALWAYS derived straight from unit_price, exactly as before
            # this whole investigation — no separate discount line, no
            # discount %, and (2026-09-21 user decision, correcting an
            # earlier version of this same fix that DID add it back) no
            # adding back whatever portion of the discount Mercado Libre
            # itself might fund either: "si nosotros subsidiamos hay que
            # bajarle al monto de la venta, si es generado por mercado
            # libre igualmente tendríamos que hacerlo sobre todo para
            # que se empate la facturación" — the sale must match
            # Mercado Libre's own CFDI, which is always stamped at
            # unit_price regardless of who funded the discount; whatever
            # Mercado Libre separately owes XE for its own funded share
            # is a settlement matter, never something that changes the
            # invoiced amount. order_items[].discounts[].amounts.full/
            # seller are captured below PURELY as informational fields
            # (meli_discount_amount/meli_discount_ml_funded_amount) —
            # visible for review, with zero effect on price_unit or the
            # sale's own total.
            unit_discount_full = 0.0
            unit_discount_seller = 0.0
            for discount in (order_item.get('discounts') or []):
                amounts = discount.get('amounts') or {}
                unit_discount_full += amounts.get('full') or 0.0
                unit_discount_seller += amounts.get('seller') or 0.0
            ml_funded_unit_amount = max(unit_discount_full - unit_discount_seller, 0.0)
            if used_minimum_price_fallback:
                price_unit = 0.01
            else:
                price_unit = self._meli_price_unit_untaxed(product, ml_unit_price or 0.0)
            quantity = order_item.get('quantity') or 1
            resolved_lines.append((
                (0, 0, {
                    'product_id': product.id,
                    'product_uom_qty': quantity,
                    'price_unit': price_unit,
                    'meli_order_id': order_id,
                    # 2026-09-21 (user request): informational only,
                    # totalled across this line's own quantity — see
                    # these two fields' own help text for why they
                    # never affect price_unit/the sale's own total.
                    'meli_discount_amount': unit_discount_full * quantity,
                    'meli_discount_ml_funded_amount': ml_funded_unit_amount * quantity,
                }),
                {
                    'sku': sku or item.get('id') or '?',
                    'product_id': product.id,
                    'ml_unit_price': ml_unit_price,
                    'price_unit': price_unit,
                    'used_payments_fallback': used_payments_fallback,
                    'used_minimum_price_fallback': used_minimum_price_fallback,
                },
            ))
        return resolved_lines, unmapped_skus

    def _meli_notify_price_fallbacks(self, price_debug):
        """Posts the chatter/manager notifications for any price
        fallback _meli_build_order_lines had to use for this order —
        see that method's own docstring (Fix 2026-09-18) for why either
        fallback can happen at all. Called once per _meli_build_order_
        lines call (initial creation and each pack sibling addition).
        """
        self.ensure_one()
        payments_fallback_skus = [
            debug['sku'] for debug in price_debug if debug.get('used_payments_fallback')
        ]
        minimum_price_fallback_skus = [
            debug['sku'] for debug in price_debug if debug.get('used_minimum_price_fallback')
        ]
        if payments_fallback_skus:
            self.message_post(body=_(
                "Mercado Libre reported no per-item price for: "
                "%(skus)s (a known gap for 'meli_resale'/catalog "
                "orders — unit_price comes back null). The price used "
                "instead was computed from this order's own total "
                "payments, split evenly across its item(s) — there is "
                "no way to know the real per-SKU breakdown when this "
                "happens."
            ) % {'skus': ', '.join(payments_fallback_skus)})
        if minimum_price_fallback_skus:
            self._meli_notify_queue_job_managers(_(
                "Mercado Libre reported no usable price at all "
                "(neither unit_price nor any payment amount) for: "
                "%(skus)s. Priced at $0.01 so the order could still be "
                "created — review and correct the price manually."
            ) % {'skus': ', '.join(minimum_price_fallback_skus)})

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
    meli_discount_amount = fields.Monetary(
        string='Mercado Libre Discount', copy=False,
        currency_field='currency_id',
        help="Total coupon/promotion discount (order_items[].discounts[]."
             "amounts.full, summed across every discount entry and "
             "multiplied by this line's own quantity) Mercado Libre "
             "applied to this product — informational only, never "
             "affects price_unit or the sale's own total. price_unit "
             "already reflects the correct net price XE is actually "
             "paid for (see _meli_build_order_lines): this field exists "
             "purely so a discounted line is identifiable and its real "
             "discount amount visible, the same way Ventiapp shows it, "
             "without folding it into the price itself. 0 when no "
             "discount applied — see meli_discount_ml_funded_amount for "
             "the part of it, if any, Mercado Libre itself subsidizes "
             "rather than XE.",
    )
    meli_discount_ml_funded_amount = fields.Monetary(
        string='Mercado Libre-Funded Discount', copy=False,
        currency_field='currency_id',
        help="The portion of meli_discount_amount that Mercado Libre "
             "itself (or a brand/campaign) funds rather than XE — "
             "order_items[].discounts[].amounts.full minus amounts."
             "seller, summed and multiplied by quantity. price_unit "
             "already adds this back on top of Mercado Libre's own "
             "unit_price (which only reflects what the BUYER paid), so "
             "XE's real receivable is correct regardless — this field "
             "exists purely to make that adjustment visible/auditable. "
             "0 whenever XE funds the whole discount itself (the "
             "ordinary case so far — every real order checked to date, "
             "2026-09-21, has amounts.seller == amounts.full).",
    )

    # Fix 2026-09-22 round 6 (user-directed, real regression round 5's
    # own fix introduced — caught by re-running the test suite, order
    # S955446): round 5 removed sale_line_ids from a DISCOUNT-type
    # credit note line to stop sale.order.line.qty_invoiced from
    # wrongly netting it — but sale.order.invoice_ids itself (Odoo
    # core, sale_order.py _get_invoiced: "order.order_line.invoice_
    # lines.move_id") is ALSO computed purely from that same relation.
    # Removing sale_line_ids didn't just fix qty_invoiced — it made the
    # whole credit note invisible to invoice_ids too, which this very
    # module's own source_invoice/existing_refund searches (and the
    # "Facturas" smart button in the UI) depend on completely. A
    # discount credit note built that way could never be found again
    # on the very next reconcile call, and would keep trying to
    # recreate itself. sale_line_ids is kept intact now (restored); the
    # netting fix instead targets qty_invoiced directly below, scoped
    # only to a line explicitly flagged account.move.line.meli_
    # discount_adjustment (see that field's own docstring further down
    # this file).
    @api.depends('invoice_lines.meli_discount_adjustment')
    def _compute_qty_invoiced(self):
        super()._compute_qty_invoiced()
        for line in self:
            for invoice_line in line._get_invoice_lines():
                if not invoice_line.meli_discount_adjustment:
                    continue
                if (
                    invoice_line.move_id.state == 'cancel'
                    and invoice_line.move_id.payment_state != 'invoicing_legacy'
                ):
                    continue
                if invoice_line.move_id.move_type != 'out_refund':
                    continue
                # Core's own _compute_qty_invoiced (see its own
                # docstring, sale/models/sale_order_line.py) already
                # subtracted this discount line's own quantity — add
                # it right back: a discount-type credit note never
                # represents a returned unit, so it must never reduce
                # qty_invoiced (see meli_discount_adjustment's own
                # docstring for the real risk that netting it creates).
                line.qty_invoiced += invoice_line.product_uom_id._compute_quantity(
                    invoice_line.quantity, line.product_uom,
                )


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    meli_discount_adjustment = fields.Boolean(
        copy=False,
        help="Set on an out_refund invoice line built by _meli_build_"
             "partial_credit_note/_meli_relate_partial_cancellation_"
             "credit_note for a genuine DISCOUNT-type partial refund — "
             "same quantity as the sale, no units physically returned, "
             "Mercado Libre's own CFDI amount less than quantity × the "
             "sale line's unit price (see either method's own docstring, "
             "fix 2026-09-22 round 4/5/6). sale.order.line._compute_qty_"
             "invoiced is overridden to ignore this line's own quantity "
             "when this is True — a discount must credit money without "
             "ever making Odoo think a unit came back, which would risk "
             "an automatic second invoice for an already fully-delivered "
             "product (see that override's own docstring). Never set for "
             "a genuine physical return, which should net qty_invoiced "
             "down exactly like core Odoo already does.",
    )
