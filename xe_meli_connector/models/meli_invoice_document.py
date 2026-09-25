import base64
import logging
from datetime import datetime

from odoo import _, api, fields, models

import pytz
import requests
from lxml import etree

from odoo.addons.queue_job.exception import RetryableJobError

from .sale_order import MELI_FISCAL_TIMEZONE

_logger = logging.getLogger(__name__)

# Confirmed against the official Mercado Libre docs (2026-09-03,
# "Descargar Facturas - Emisión MELI"): for MLM, these transaction_type
# values produce a Nota de Crédito; everything else (sale, resale,
# disposal_sale, loan) produces a Factura.
MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES = {
    'devolution', 'resale_devolution', 'disposal_sale_return',
}

# All transaction_type values documented for MLM — used by the date-range
# recovery wizard to discover every document type an order might have,
# since there is no "list all documents for this order" endpoint. Kept
# complete on purpose (2026-09-04): even though disposal_sale, loan and
# disposal_sale_return have never appeared in this account's real data,
# the user does not want the recovery wizard blind to a document type
# this account could start generating in the future.
MELI_INVOICE_TRANSACTION_TYPES = (
    'sale', 'resale', 'disposal_sale', 'loan',
    'devolution', 'resale_devolution', 'disposal_sale_return',
)

# Final review fix (2026-09-08): the 'status' field's own help text and
# this module's tree/search views (decoration-danger, the
# "cancelled_or_rejected" filter in meli_invoice_document_views.xml) are
# the two places that already treat these two raw values as the ones a
# real, live document never carries — 'rejected'/'cancelled' both mean
# the SAT/Mercado Libre itself killed this specific document. A
# devolución document in either of these statuses must never be used to
# create a real Odoo credit note — see sale.order._meli_reconcile_invoicing
# and _meli_relate_partial_cancellation_credit_note, whose own
# credit-note document searches both filter these out.
#
# Fix 2026-09-18 (real production bug, 20 real orders confirmed —
# S967516/doc 1435 among them): 'canceled' (one 'l', the actual value
# Mercado Libre's own API sends) was missing — only the British
# 'cancelled' spelling was ever listed, so every one of these filters
# silently let a real cancelled document straight through, on both the
# credit-note side AND (see the new filter added to the plain-invoice
# search below) the refacturación side.
MELI_INVOICE_DEAD_STATUSES = {'rejected', 'cancelled', 'canceled'}

# Fix 2026-09-20 (user-directed, real production case: 98
# 'pending_authorization' + 6 'interrupted' documents found in this
# account's own real data, none of them dead per MELI_INVOICE_DEAD_
# STATUSES above, all of them equally usable to create a real Odoo
# invoice/credit note before this fix — even though neither is a final
# state. 'pending_authorization' means the SAT/PAC hasn't authorized
# this CFDI yet (it can still end up 'authorized', or fail into
# 'rejected'/'cancelled'/'canceled'); 'interrupted' means the stamping
# process itself broke before finishing. Only 'authorized' is the
# genuinely final, safe-to-use state — everything else (these two, or
# no status at all) means "not ready to decide yet". Deliberately kept
# SEPARATE from MELI_INVOICE_DEAD_STATUSES: a document in one of these
# two statuses is not dead — it may still become usable — so it must
# stay visible/trackable (never excluded from a document LIST/search
# the way a dead one is), only excluded from the specific "is this
# document ready to create a real invoice/credit note from" gates.
# A document with no status at all (status=False — e.g. one fetched by
# the date-range recovery wizard, which never learns this field at
# all; see that field's own help text) is NOT included here: there is
# no way to tell such a document apart from a genuinely 'authorized'
# one, so it keeps being treated as ready, exactly as before this fix.
MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES = {'pending_authorization', 'interrupted'}

class MeliInvoiceDocument(models.Model):
    _name = 'meli.invoice.document'
    _description = 'Mercado Libre Invoice/Credit Note (native invoicer XML)'
    _order = 'create_date desc'

    meli_invoice_id = fields.Char(
        string='Mercado Libre Invoice ID', copy=False,
        help="Only known when this document arrived via the 'invoices' "
             "webhook — the date-range recovery wizard fetches straight "
             "by order_id/transaction_type and never learns this id.",
    )
    meli_order_id = fields.Char(string='Mercado Libre Order ID', copy=False)
    transaction_type = fields.Char(
        string='Transaction Type',
        help="Raw value from Mercado Libre: sale, resale, disposal_sale, "
             "loan, devolution, resale_devolution or disposal_sale_return.",
    )
    document_type = fields.Selection([
        ('factura', 'Invoice'),
        ('nota_de_credito', 'Credit Note'),
    ], string='Document Type')
    status = fields.Char(
        string='Status',
        help="Mercado Libre's own status for this document — seen in "
             "practice (2026-09-04): 'authorized'. The seller-panel UI "
             "also shows 'rechazado'/'cancelado' as possible states "
             "(a document can be cancelled outright by Mercado Libre, "
             "independent of transaction_type — noticed by the user "
             "2026-09-04: not every reversal shows up as a "
             "nota_de_credito). Only known when this document arrived "
             "via the 'invoices' webhook (the metadata call that "
             "provides it) — never populated for documents fetched by "
             "the date-range recovery wizard, which only gets the raw "
             "XML, not this metadata.",
    )
    sale_order_id = fields.Many2one(
        'sale.order', string='Sale Order', compute='_compute_sale_order_id',
        store=True,
        help="Matched by meli_order_id first (orders imported by "
             "xe_meli_connector), falling back to sale.order.line."
             "meli_order_id (a consolidated pack sibling other than the "
             "first one, whose own order id lives only on the line it "
             "added), then to client_order_ref/reference (today's "
             "Ventiapp-created orders, which don't populate meli_order_id "
             "at all), and finally to meli_pack_id — covers a resale/pack "
             "invoice whose own metadata reports the pack's id rather "
             "than one specific sibling order's id.",
    )
    company_id = fields.Many2one(
        'res.company', string='Company', related='sale_order_id.company_id',
        store=True,
    )
    meli_has_refacturacion = fields.Boolean(
        string='Refactured Sale', related='sale_order_id.meli_has_refacturacion',
        store=True,
        help="This document's sale order has more than one 'factura' "
             "document — see sale.order.meli_has_refacturacion.",
    )
    meli_has_credit_note = fields.Boolean(
        string='Sale With Credit Note',
        related='sale_order_id.meli_has_credit_note', store=True,
        help="This document's sale order has at least one live credit "
             "note — see sale.order.meli_has_credit_note.",
    )
    meli_pack_id = fields.Char(
        string='Mercado Libre Pack ID', related='sale_order_id.meli_pack_id',
        store=True,
        help="The linked sale order's own pack id, when it's part of a "
             "cart — kept here too (not just on sale.order) so this "
             "document can be found by pack id directly, without first "
             "having to open its sale order. Found in practice "
             "2026-09-08: a resale pack can be invoiced as ONE document "
             "covering every sibling order, so searching by pack id is "
             "often the only way to see 'everything for this cart'.",
    )
    meli_linked_pack_id = fields.Char(
        string='Linked Pack ID', copy=False,
        help="A pack id known for this document by some other means than "
             "the tiers _compute_sale_order_id already tries on its own — "
             "the main real case (2026-09-14) is an order adopted from "
             "Ventiapp: its sale.order only ever knows its own pack_id "
             "(reference/client_order_ref/meli_pack_id), never this "
             "document's own real meli_order_id, so none of the existing "
             "tiers can bridge the two. Deliberately source-agnostic: "
             "filled today via the Order-to-Pack Excel mapping wizard, "
             "but nothing here assumes that — a future automatic lookup "
             "(e.g. fetching the order's own pack_id straight from "
             "Mercado Libre) can populate it the exact same way.",
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Warehouse', related='sale_order_id.warehouse_id',
        store=True,
    )
    meli_warehouse_kind = fields.Selection([
        ('full', 'Full (Mercado Libre)'),
        ('default', 'Monterrey XE2'),
    ], string='Warehouse Kind', compute='_compute_meli_warehouse_kind', store=True,
        help="Classifies the linked sale order's warehouse against this "
             "company's own meli.config (warehouse_fulfillment_id vs. "
             "warehouse_default_id) so documents can be filtered/grouped "
             "by Full vs. Monterrey XE2 — same criterion sale.order's own "
             "is_full check already uses. Empty until sale_order_id "
             "resolves, or if the warehouse matches neither configured "
             "one.",
    )
    issue_date = fields.Datetime(
        string='Issue Date',
        help="Parsed straight from the CFDI's own Fecha attribute (the "
             "Comprobante root node) — never from Mercado Libre's invoices "
             "metadata endpoint, which is only ever fetched on the webhook "
             "path. Reading it from the XML itself means it's populated "
             "for every document without exception, including ones the "
             "date-range recovery wizard fetched and that never had "
             "metadata to begin with.",
    )
    xml_filename = fields.Char(string='XML Filename')
    xml_file = fields.Binary(string='XML File')
    last_synced_at = fields.Datetime(string='Last Synced At')

    # Fix 2026-09-11: sale_order_id being set only means this document was
    # matched to the right SALE — it says nothing about whether it was
    # ever actually turned into a real Odoo invoice/credit note. Found in
    # practice: a credit note document can sit with sale_order_id already
    # correct for a long time while _meli_reconcile_invoicing's own
    # sibling-line match keeps failing (see
    # sale.order.action_meli_retry_invoicing_reconciliation's own
    # docstring), with nothing on this record itself showing that it's
    # stuck. account.move.meli_invoice_document_id (set only by
    # sale.order._meli_relate_invoice_document, the one place that
    # actually applies a document) is the real "was this ever applied"
    # signal — move_ids is its inverse here.
    move_ids = fields.One2many(
        'account.move', 'meli_invoice_document_id',
        string='Related Invoice/Credit Note',
        help="The real Odoo invoice/credit note this document was "
             "actually applied to. Empty means nothing has been created "
             "in Odoo for it yet — even if Sale Order above is already "
             "set, e.g. a credit note whose own sibling line couldn't be "
             "matched at the time and is stuck waiting on a manual "
             "retry (Sale Order's own 'Retry Invoicing' button).",
    )
    is_applied = fields.Boolean(
        string='Applied in Odoo', compute='_compute_is_applied', store=True,
        help="True once move_ids is non-empty — see that field's own "
             "help text for what 'applied' means here.",
    )
    meli_has_xml = fields.Boolean(
        string='Has XML', compute='_compute_meli_has_xml', store=True,
        help="True once xml_file is set. A Binary field stored as an "
             "attachment doesn't reliably support a plain domain search "
             "on its own (e.g. in a search-view filter or a cron's own "
             "search()) — this stored boolean exists purely so 'missing "
             "XML file' can be filtered/searched for.",
    )
    meli_sale_order_state = fields.Selection(
        related='sale_order_id.state', string='Sale Order State',
        help="Shows the 'Retry Confirmation' button only when the "
             "related sale is stuck in draft (2026-09-14 user request: "
             "some scenario left the order created but unconfirmed, so "
             "nothing got delivered/invoiced automatically).",
    )
    meli_xml_total = fields.Float(
        string='Invoiced Total (XML)', compute='_compute_meli_xml_total',
        store=True, digits=(16, 2),
        help="The Total attribute stamped on the CFDI itself (root "
             "cfdi:Comprobante node) — the real fiscal total Mercado "
             "Libre invoiced, independent of whatever Odoo's own sale "
             "order computed. 0.0 when there's no XML yet.",
    )
    meli_amount_mismatch = fields.Boolean(
        string='Invoice/Sale Amount Mismatch', compute='_compute_meli_amount_mismatch',
        store=True,
        help="True when meli_xml_total and the related sale order's own "
             "amount_total differ by more than 5 cents (2026-09-14 "
             "user request) — filterable here so these can be found and "
             "reviewed manually. Fix 2026-09-24 (user-directed): always "
             "False while meli_order_has_partial_refund is True — see "
             "that field's own help text for why that gap is expected "
             "by design there, never an error to review.",
    )
    meli_move_amount_mismatch = fields.Boolean(
        string='Invoice/CFDI Amount Mismatch', compute='_compute_meli_amount_mismatch',
        store=True,
        help="True when the REAL Odoo invoice/credit note this document "
             "was applied to (move_ids) doesn't add up to meli_xml_total "
             "(more than 5 cents off) — a different, more serious gap "
             "than meli_amount_mismatch: that one only ever compares the "
             "XML against the SALE's own total (which can already "
             "include a pack sibling's line added after this move was "
             "posted), never against what was actually invoiced/"
             "credited. Real production case: order S974870/pack "
             "2000015124781237 — a second pack sibling's own line "
             "wasn't delivered yet when the invoice was first built, so "
             "only one of its two products ever made it onto the "
             "posted invoice and its credit note, both silently short "
             "by that missing line's own amount, with nothing else ever "
             "catching or correcting it. False whenever move_ids is "
             "empty (nothing posted yet to compare).",
    )
    meli_order_has_partial_refund = fields.Boolean(
        string='Order Has Partial Refund', compute='_compute_meli_amount_mismatch',
        store=True,
        help="True when the related sale order's own cached status "
             "(sale_order_id.meli_last_status) is 'partially_refunded' "
             "— the most common real explanation for meli_amount_"
             "mismatch on a 'factura' document: the sale's own "
             "amount_total never changes for a partial refund (only a "
             "credit note is issued for it), so it will keep differing "
             "from the invoice's own XML total by design, not by error. "
             "Read from the locally-cached status (refreshed on every "
             "order-status notification/poll), not a live API call, so "
             "it can lag briefly behind Mercado Libre's own real-time "
             "status — same tradeoff meli_last_status itself already "
             "has everywhere else in this module.",
    )
    meli_amount_mismatch_notified = fields.Boolean(
        default=False, copy=False,
        help="Internal: set once the chatter warning for a detected "
             "amount mismatch has been posted, so _meli_upsert never "
             "posts it more than once for the same document.",
    )
    meli_needs_manual_credit_note = fields.Boolean(
        string='Needs Manual Credit Note', default=False, copy=False,
        help="True for a devolution/credit-note document that couldn't "
             "be applied automatically: either its order's own LIVE "
             "Mercado Libre status is neither 'cancelled' nor "
             "'partially_refunded' yet (nothing confirmed to act on), "
             "or it IS a confirmed partial refund but its own CFDI "
             "concept(s) couldn't be confidently matched to one exact "
             "invoice line/quantity — apply it manually for the exact "
             "product/quantity/amount its own XML reports, without "
             "touching stock or the sale. Also excludes this document "
             "from the 'Has Sale But Not Applied' automatic retry cron "
             "(_cron_retry_unapplied_documents) — it will never become "
             "'Applied in Odoo' on its own, so retrying it automatically "
             "would just repeat the same manual-review chatter message "
             "every 30 minutes forever.",
    )
    meli_needs_manual_mismatch_review = fields.Boolean(
        string='Needs Manual Mismatch Review', default=False, copy=False,
        help="True for a 'factura' document whose own XML total doesn't "
             "match its related sale order's amount_total (same 5-cent "
             "tolerance as meli_amount_mismatch) at the exact moment "
             "sale.order._meli_reconcile_invoicing would otherwise have "
             "created and posted the initial invoice from it (2026-09-18, "
             "user decision): creating a real, CFDI-related invoice for "
             "an amount that doesn't match what was actually sold is "
             "deliberately paused — apply it manually once the "
             "underlying cause (most commonly a still-missing pack "
             "sibling line — see sale.order._meli_ensure_all_pack_"
             "siblings_imported) is resolved and the totals agree. Also "
             "excludes this document from the 'Has Sale But Not Applied' "
             "automatic retry cron (_cron_retry_unapplied_documents), "
             "same reasoning as meli_needs_manual_credit_note: retrying "
             "automatically would just repeat the same manual-review "
             "chatter message every 30 minutes for as long as the "
             "mismatch persists. Deliberately does NOT gate credit-note "
             "relating/stock-return at all — that side of the pipeline "
             "is a separate, not-yet-designed piece of this same policy "
             "(see docs/superpowers — 2026-09-18 conversation).",
    )
    meli_stock_return_pending = fields.Boolean(
        string='Stock Return Pending', compute='_compute_meli_stock_return_pending',
        search='_search_meli_stock_return_pending',
        help="True for a credit-note document that IS already Applied in "
             "Odoo (its own credit note was related) but the pack "
             "sibling it belongs to still has delivered stock that was "
             "never actually returned (2026-09-16, real production bug, "
             "order S841005/pack 2000014914865611): relating the credit "
             "note and physically returning the stock are two separate "
             "steps, and Applied in Odoo only ever tracks the first one "
             "— see sale.order.action_meli_retry_invoicing_"
             "reconciliation's own docstring for the full explanation. "
             "Not stored (always computed fresh from the real stock "
             "moves, never a flag that could go stale) — filterable "
             "here to find these; clicking 'Retry Invoicing' on the "
             "related Sale Order (or waiting for the 30-minute backup "
             "cron) resolves it.",
    )

    meli_sale_delivered = fields.Boolean(
        string='Sale Delivered', compute='_compute_meli_sale_delivered',
        search='_search_meli_sale_delivered',
        help="True once at least one unit of this document's own sale "
             "has actually left the warehouse (a done outgoing stock "
             "move) — scoped to this document's own sibling within a "
             "pack when it belongs to one, or the whole order "
             "otherwise; True regardless of whether that stock was "
             "later returned. 2026-09-21 user request: 'Has Sale But "
             "Not Applied' (see is_applied) lumps together several "
             "different reasons a document never got a real invoice/"
             "credit note; the most common one for a still-fresh order "
             "is simply that nothing has been delivered yet, so Odoo's "
             "own delivery-based invoicing policy has nothing to "
             "invoice yet. False here on a 'Has Sale But Not Applied' "
             "document narrows it down to exactly that cause — see the "
             "'Not Applied: Nothing Delivered Yet' filter. Not stored, "
             "same reasoning as meli_stock_return_pending's own help "
             "text: always computed fresh from the real stock moves, "
             "never a flag that could go stale.",
    )

    @api.depends('move_ids')
    def _compute_is_applied(self):
        for document in self:
            document.is_applied = bool(document.move_ids)

    def _compute_meli_sale_delivered(self):
        for document in self:
            delivered = False
            if document.sale_order_id:
                order = document.sale_order_id
                lines = order._meli_sibling_lines(document.meli_order_id)
                if lines:
                    delivered_qty, __ = order._meli_sibling_delivered_and_returned_qty(lines)
                    delivered = delivered_qty > 0
            document.meli_sale_delivered = delivered

    def _search_meli_sale_delivered(self, operator, value):
        # Non-stored (see the field's own help text) — evaluated in
        # Python over the small set of documents that could possibly
        # qualify, same convention as _search_meli_stock_return_pending.
        want = bool(value) if operator == '=' else not bool(value)
        candidates = self.search([('sale_order_id', '!=', False)])
        matching_ids = candidates.filtered(
            lambda d: d.meli_sale_delivered
        ).ids
        return [('id', 'in' if want else 'not in', matching_ids)]

    def _compute_meli_stock_return_pending(self):
        for document in self:
            pending = False
            if (
                document.is_applied
                and document.meli_pack_id
                and document.sale_order_id
                and document.transaction_type in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
                # Fix 2026-09-22 (real production bug, user-caught: a
                # document could show BOTH "Order Has Partial Refund"
                # and "Stock Return Pending" checked at once — a
                # confirmed partial refund (see sale.order._meli_build_
                # partial_credit_note/_meli_relate_partial_cancellation_
                # credit_note's own discount-type branch, account.move.
                # line.meli_discount_adjustment) never touches stock at
                # all — nothing was ever delivered-and-not-returned to
                # begin with, so "pending" makes no sense for it.
                # delivered_qty > returned_qty below is naturally True
                # for a discount (delivered=1, returned=0), even though
                # this was never a physical return — checked against
                # the credit note's OWN posted move, not the order's
                # possibly-stale cached status, since a pack can have
                # siblings in different states at once.
                and not any(
                    document.move_ids.filtered(lambda m: m.state == 'posted')
                    .invoice_line_ids.mapped('meli_discount_adjustment')
                )
            ):
                order = document.sale_order_id
                lines = order._meli_sibling_lines(document.meli_order_id)
                if lines:
                    delivered_qty, returned_qty = (
                        order._meli_sibling_delivered_and_returned_qty(lines)
                    )
                    pending = delivered_qty > returned_qty
            document.meli_stock_return_pending = pending

    def _search_meli_stock_return_pending(self, operator, value):
        # Non-stored on purpose (see the field's own help text) — this
        # can't be expressed as a plain SQL domain, so it's evaluated in
        # Python over the small set of documents that could possibly
        # qualify (applied pack credit notes only), same convention as
        # any other non-stored searchable field in Odoo.
        want = bool(value) if operator == '=' else not bool(value)
        candidates = self.search([
            ('is_applied', '=', True),
            ('meli_pack_id', '!=', False),
            ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
        ])
        matching_ids = candidates.filtered(
            lambda d: d.meli_stock_return_pending
        ).ids
        return [('id', 'in' if want else 'not in', matching_ids)]

    @api.depends('xml_file')
    def _compute_meli_has_xml(self):
        for document in self:
            document.meli_has_xml = bool(document.xml_file)

    @api.depends('xml_file')
    def _compute_meli_xml_total(self):
        for document in self:
            xml_total = False
            if document.xml_file:
                xml_total = self._meli_parse_total_from_xml(
                    base64.b64decode(document.xml_file)
                )
            document.meli_xml_total = xml_total or 0.0

    @api.depends(
        'meli_xml_total', 'meli_has_xml', 'sale_order_id.amount_total',
        'sale_order_id.meli_last_status', 'move_ids.amount_total', 'move_ids.state',
    )
    def _compute_meli_amount_mismatch(self):
        for document in self:
            document.meli_order_has_partial_refund = (
                document.sale_order_id.meli_last_status == 'partially_refunded'
            )
            document.meli_amount_mismatch = bool(
                not document.meli_order_has_partial_refund
                and document.meli_has_xml and document.sale_order_id
                and abs(document.meli_xml_total - document.sale_order_id.amount_total) > 0.05
            )
            live_moves = document.move_ids.filtered(lambda m: m.state != 'cancel')
            document.meli_move_amount_mismatch = bool(
                document.meli_has_xml and live_moves
                and abs(sum(live_moves.mapped('amount_total')) - document.meli_xml_total) > 0.05
            )

    _sql_constraints = [(
        'invoice_id_uniq', 'unique(meli_invoice_id)',
        'This Mercado Libre invoice is already registered.',
    )]

    @api.depends('company_id', 'warehouse_id')
    def _compute_meli_warehouse_kind(self):
        Config = self.env['meli.config'].sudo()
        configs_by_company = {}
        for document in self:
            kind = False
            if document.warehouse_id and document.company_id:
                config = configs_by_company.get(document.company_id.id)
                if config is None:
                    config = Config.search(
                        [('company_id', '=', document.company_id.id)], limit=1,
                    )
                    configs_by_company[document.company_id.id] = config
                if config:
                    if document.warehouse_id == config.warehouse_fulfillment_id:
                        kind = 'full'
                    elif document.warehouse_id == config.warehouse_default_id:
                        kind = 'default'
            document.meli_warehouse_kind = kind

    @api.depends('meli_order_id', 'meli_linked_pack_id')
    def _compute_sale_order_id(self):
        SaleOrder = self.env['sale.order']
        SaleOrderLine = self.env['sale.order.line']
        for document in self:
            order = SaleOrder.browse()
            if document.meli_order_id:
                order = SaleOrder.search(
                    [('meli_order_id', '=', document.meli_order_id)], limit=1,
                )
                if not order:
                    # A pack sibling other than the first one: after
                    # consolidation, its own order id only lives on the
                    # sale.order.line(s) it added, never on the
                    # sale.order itself (which only ever remembers the
                    # FIRST sibling's meli_order_id plus the shared
                    # meli_pack_id) — see
                    # sale.order._meli_add_pack_sibling_lines.
                    line = SaleOrderLine.sudo().search(
                        [('meli_order_id', '=', document.meli_order_id)], limit=1,
                    )
                    order = line.order_id
                if not order:
                    order = SaleOrder.search([
                        '|',
                        ('client_order_ref', '=', document.meli_order_id),
                        ('reference', '=', document.meli_order_id),
                    ], limit=1)
                if not order:
                    # Last resort: this document's own "order id" is
                    # actually a pack id — not expected from either known
                    # fetch path today, but cheap insurance against a
                    # future metadata shape reporting the pack instead of
                    # one specific sibling order.
                    order = SaleOrder.search(
                        [('meli_pack_id', '=', document.meli_order_id)], limit=1,
                    )
            if not order and document.meli_linked_pack_id:
                # Bridges the gap the tiers above can't: a Ventiapp-adopted
                # order's sale.order only ever knows its own pack_id, never
                # this document's real meli_order_id — see
                # meli_linked_pack_id's own help text.
                order = SaleOrder.search(
                    [('meli_pack_id', '=', document.meli_linked_pack_id)], limit=1,
                )
            document.sale_order_id = order

    def _meli_recompute_and_reconcile(self):
        """Shared core: forces a recompute of sale_order_id on `self`
        (expected to already be filtered to documents with
        sale_order_id = False — this method doesn't filter itself,
        both call sites already search for exactly that), then
        immediately reconciles invoicing for any that newly resolve —
        mirrors _meli_upsert's own reconcile-on-resolve behavior for
        the ordinary, non-orphaned case.

        sale_order_id is a stored compute field with
        @api.depends('meli_order_id') only — its own field, never
        anything about sale.order — so Odoo's dependency tracking has
        no way to know to revisit it just because a matching sale.order
        shows up later. Before this existed (2026-09-10), only one call
        site (sale.order._meli_recover_cancelled_on_arrival_full,
        2026-09-09) ever forced this recompute, and only for its own
        narrow case (a Full order recovered from arriving already
        'cancelled'). A document that arrived before a completely
        NORMAL order got created (paid, never cancelled) had nothing
        forcing its own recompute — confirmed in practice (2026-09-10):
        a real document sat with sale_order_id blank until something
        unrelated coincidentally forced a recompute.
        """
        if not self:
            return
        self._compute_sale_order_id()
        for document in self.filtered('sale_order_id'):
            try:
                with self.env.cr.savepoint():
                    # Fix 2026-09-24 (Monterrey XE2 total-cancellation
                    # project, Phase 1) — see the other call site's
                    # identical comment, in _meli_upsert, for why this
                    # is needed here too.
                    if document.transaction_type in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES:
                        document.sale_order_id._meli_infer_cancelled_from_credit_note()
                    document.sale_order_id._meli_reconcile_invoicing()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: invoicing reconciliation "
                    "failed after a previously-orphaned document %s was "
                    "relinked — left pending for manual review.",
                    document.sale_order_id.client_order_ref, document.id,
                )

    def _meli_assign_linked_pack_id(self, pack_id):
        """Sets meli_linked_pack_id and, when that resolves this
        document's sale_order_id for the first time, immediately
        reconciles invoicing — mirrors _meli_upsert's own
        reconcile-on-resolve behavior. Returns whether the document ends
        up related to a sale order. A no-op (but still returns the
        current state) when the value hasn't changed, so the caller (the
        Order-to-Pack Excel wizard) can re-run the same file without
        re-triggering reconciliation on every row every time.
        """
        self.ensure_one()
        if self.meli_linked_pack_id == pack_id:
            return bool(self.sale_order_id)
        self.write({'meli_linked_pack_id': pack_id})
        if self.sale_order_id:
            try:
                with self.env.cr.savepoint():
                    self.sale_order_id._meli_reconcile_invoicing()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: invoicing reconciliation "
                    "failed after document %s was linked to pack %s via "
                    "manual mapping — left pending for manual review.",
                    self.sale_order_id.client_order_ref, self.id, pack_id,
                )
        return bool(self.sale_order_id)

    @api.model
    def _meli_relink_orphaned_documents(self, order_id, pack_id=False):
        """Immediate fix: called from sale.order._meli_import_order,
        right after it creates/resolves an order, scoped to exactly
        that order's own order_id/pack_id — covers the vast majority of
        cases with no delay. See _meli_recompute_and_reconcile for the
        actual mechanism and why this is needed at all.
        """
        orphaned_documents = self.sudo().search([
            ('meli_order_id', 'in', ([pack_id, order_id] if pack_id else [order_id])),
            ('sale_order_id', '=', False),
        ])
        orphaned_documents._meli_recompute_and_reconcile()

    @api.model
    def _cron_relink_orphaned_documents(self):
        """10-minute safety-net cron (xe_meli_connector/data/ir_cron.xml)
        for whatever _meli_relink_orphaned_documents's own immediate
        call site doesn't catch — same "primary path + backup cron"
        pattern already used everywhere else in this module for
        orders/claims/invoices polling. Cheap: no Mercado Libre API
        calls at all, pure ORM, so a short interval costs nothing.
        """
        orphaned_documents = self.sudo().search([
            ('sale_order_id', '=', False),
            ('meli_order_id', '!=', False),
        ])
        orphaned_documents._meli_recompute_and_reconcile()

    @api.model
    def _cron_retry_unapplied_documents(self):
        """30-minute safety-net cron (xe_meli_connector/data/ir_cron.xml)
        equivalent to a human clicking sale.order's own "Retry Invoicing
        Reconciliation" button on every order that still has one of
        these stuck (2026-09-11): a document already related to its
        sale (sale_order_id set) that never actually got applied
        (is_applied still False — see that field's own help text for
        the most common real cause, an order adopted from Ventiapp,
        whose lines never carry Mercado Libre's own order id at all).

        Deliberately calls the SAME action_meli_retry_invoicing_
        reconciliation the button uses — not some looser/eager sweep —
        so it inherits every one of that method's own safety gates
        (Full-only, pack-only for the fuller stock-return/quantity
        step) without duplicating them here. Each order is retried in
        its own try/except: one order raising an unexpected error must
        not block every other stuck order in this run.

        Fix 2026-09-16 (real production bug, order S841005/pack
        2000014914865611): is_applied alone isn't enough to find every
        pack order this cron needs to touch — a pack sibling's credit
        note can be is_applied=True (related) while its own delivered
        stock was never actually returned (see sale.order.
        action_meli_retry_invoicing_reconciliation's own updated
        docstring for the full explanation: relating the credit note and
        returning the stock are two separate halves of the same job).
        The second branch below also picks up any PACK order with an
        applied credit-note document, so that method's own — now
        equally updated — stock-return check gets a chance to run on
        it too; that method itself is cheap and idempotent for a sibling
        that's already fully done, so re-checking an already-resolved
        pack order here costs a few no-op queries, not a repeat action.
        """
        stuck_orders = self.sudo().search([
            ('sale_order_id', '!=', False),
            # Fix 2026-09-16: a document flagged meli_needs_manual_credit_
            # note is deliberately paused, not stuck — it will never
            # become is_applied on its own (see that field's own help
            # text), so retrying it here would just repeat the exact
            # same manual-review chatter message every 30 minutes.
            ('meli_needs_manual_credit_note', '=', False),
            ('meli_needs_manual_mismatch_review', '=', False),
            '|',
                ('is_applied', '=', False),
                '&', ('meli_pack_id', '!=', False),
                     ('transaction_type', 'in', list(MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES)),
        ]).mapped('sale_order_id')
        for order in stuck_orders:
            try:
                order.action_meli_retry_invoicing_reconciliation()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: automatic retry of invoicing "
                    "reconciliation failed — needs manual review.",
                    order.client_order_ref,
                )

    @api.model
    def _cron_recover_orphaned_document_orders(self):
        """30-minute safety-net cron (xe_meli_connector/data/ir_cron.xml,
        2026-09-14 user request): catches an invoice/credit-note document
        that's STILL orphaned (sale_order_id=False) with no existing
        sale.order to relink to at all — meli.invoice.document._meli_
        upsert's own Trigger B only gets one shot at this, right when
        the document first arrives; if that attempt permanently failed
        (exhausted its own 8 retries) or the document predates Trigger B
        existing, nothing else ever revisits it. Deliberately scoped
        ONLY to documents already missing a sale order (first phase,
        explicit user request) — never a general Mercado Libre order
        poll/scan; _meli_import_order still applies its own VentiApp
        grace period (MELI_ORDER_RECOVERY_GRACE_MINUTES) and adoption
        check before creating anything.

        Known simplification: an orphaned document has no company_id of
        its own (that field is related through sale_order_id — exactly
        what's missing here), so this assumes a single connected
        meli.config (true today: XE Brands) and uses its company_id for
        every orphaned document found. Revisit if a second company ever
        gets its own Mercado Libre connection.

        Fix 2026-09-24 (real production incident, order_id
        2000000005969244, documents 15630/15678): excludes transaction_
        type 'service_test' — Mercado Libre's own connectivity test
        ping, sent periodically against a made-up order_id that will
        never exist. Without this, every cycle re-enqueues
        _meli_import_order for it, which 404s against the real API
        forever — this order_id alone had retried every 30 minutes for
        over a week straight before being noticed.
        """
        config = self.env['meli.config'].sudo().search([
            ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            return
        orphaned_order_ids = set(self.sudo().search([
            ('sale_order_id', '=', False), ('meli_order_id', '!=', False),
            ('transaction_type', '!=', 'service_test'),
        ]).mapped('meli_order_id'))
        for order_id in orphaned_order_ids:
            # priority=0 (was 8, 2026-09-24 user-directed): same
            # reasoning as Trigger B's own identical call above — this
            # connector is the only real consumer of this queue. No
            # extra eta needed here on top of it, unlike Trigger B: this
            # cron itself already only runs every 30 minutes, so a
            # document only reaches this loop after already having had
            # a full cycle for the order's own normal import to land
            # first.
            self.env['sale.order'].sudo().with_delay(
                priority=0, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_recover_order_{order_id}",
            )._meli_import_order(config.company_id.id, order_id)

    @staticmethod
    def _meli_document_type_for(transaction_type):
        return (
            'nota_de_credito'
            if transaction_type in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
            else 'factura'
        )

    def _meli_upsert(self, order_id, transaction_type, xml_bytes, meli_invoice_id=False, status=False, company_id=False):
        """Keyed primarily by meli_invoice_id when known — the only
        identifier Mercado Libre actually guarantees is unique per
        document. Falls back to (order_id, transaction_type) only to
        find a document that doesn't know its invoice_id yet (the
        order-based recovery path), or to confirm a re-processed
        notification is the SAME document.

        Confirmed with Mercado Libre support (2026-09-08, case
        480258955): a single order can legitimately get MORE than one
        document of the same transaction_type over time — e.g. a
        generic-RFC "factura global" gets cancelled and replaced by a
        new one stamped to the buyer's real RFC once they request it,
        each with its own invoice_id/UUID, the original staying on
        record as 'cancelled'. Matching by (order_id, transaction_type)
        alone (the original design) silently overwrote the first
        document with the second, losing all record that the first one
        ever existed. Now a genuine replacement — an existing record
        already has a DIFFERENT known meli_invoice_id — creates a new
        row instead of overwriting it.
        """
        meli_invoice_id = str(meli_invoice_id) if meli_invoice_id else False
        vals = {
            'meli_order_id': order_id,
            'transaction_type': transaction_type,
            'document_type': self._meli_document_type_for(transaction_type),
            'xml_filename': f"{order_id or meli_invoice_id}_{transaction_type or 'invoice'}.xml",
            'xml_file': base64.b64encode(xml_bytes) if xml_bytes else False,
            'last_synced_at': fields.Datetime.now(),
        }
        if meli_invoice_id:
            vals['meli_invoice_id'] = meli_invoice_id
        if status:
            vals['status'] = status
        if xml_bytes:
            issue_date = self._meli_parse_issue_date_from_xml(xml_bytes)
            if issue_date:
                vals['issue_date'] = issue_date

        existing = self.browse()
        if meli_invoice_id:
            existing = self.sudo().search([('meli_invoice_id', '=', meli_invoice_id)], limit=1)
        if not existing:
            same_order_type = self.sudo().search([
                ('meli_order_id', '=', order_id), ('transaction_type', '=', transaction_type),
            ], limit=1)
            # Final review fix (2026-09-08, Fix 4): the pack-based
            # reuse/fan-out below is only correct for FACTURAS — Mercado
            # Libre genuinely invoices a whole pack with ONE physical
            # CFDI, confirmed in practice (see the comment right below).
            # DEVOLUCIONES (credit notes) are NOT shared per-pack: each
            # sibling order gets its OWN, independent devolución.
            # Applying the same pack-wide fan-out to a credit-note
            # transaction_type let importing sibling B's own devolución
            # find sibling A's devolución row (matched purely via the
            # shared meli_pack_id/sale_order_id) and silently overwrite
            # it with B's own meli_order_id and XML — destroying A's own
            # credit-note record. For a credit-note-family
            # transaction_type this block is skipped entirely: only the
            # direct (meli_order_id, transaction_type) match right above
            # (this document's own order id) is ever reused; anything
            # else always creates a brand new row.
            if not same_order_type and transaction_type not in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES:
                # This order_id alone found nothing — but if it's part of
                # a pack, a SIBLING order may already have this exact
                # document recorded (Mercado Libre invoices an entire
                # pack as ONE physical CFDI; confirmed in practice
                # 2026-09-07: 10+ groups of documents in production share
                # the identical stored XML file, one per sibling order,
                # because neither side knew meli_invoice_id yet to
                # de-duplicate by that alone). Resolve the sale.order
                # this order_id belongs to (which, once consolidated,
                # IS the pack's single sale.order) and look for an
                # existing document of the same transaction_type there.
                sale_order = self.env['sale.order']._meli_find_order_by_id_or_pack(order_id)
                if sale_order:
                    domain = [('transaction_type', '=', transaction_type)]
                    if sale_order.meli_pack_id:
                        # The resolver's own tier 1 (exact meli_order_id)
                        # always matches THIS order_id's own sale.order
                        # first, which is of no help by itself — the
                        # sibling's document was recorded against a
                        # DIFFERENT sale.order row entirely (each legacy
                        # sibling order still gets its own row today).
                        # meli_pack_id is what actually ties them
                        # together: it's stored (related from
                        # sale_order_id.meli_pack_id) on every document
                        # precisely so it can be found this way.
                        domain += [
                            '|',
                            ('sale_order_id', '=', sale_order.id),
                            ('meli_pack_id', '=', sale_order.meli_pack_id),
                        ]
                    else:
                        domain.append(('sale_order_id', '=', sale_order.id))
                    same_order_type = self.sudo().search(domain, limit=1)
            # Reuse that record only if it's the SAME document: either
            # side doesn't know an invoice_id yet, or both agree on it.
            # Never overwrite a record already identified as a
            # DIFFERENT invoice_id — that's a replacement, not an update.
            if same_order_type and (
                not same_order_type.meli_invoice_id
                or not meli_invoice_id
                or same_order_type.meli_invoice_id == meli_invoice_id
            ):
                existing = same_order_type

        if existing:
            existing.write(vals)
            document = existing
        else:
            document = self.sudo().create(vals)

        if not document.sale_order_id and order_id and company_id:
            # Trigger B (2026-09-09, cancelled-order-recovery plan): this
            # document arrived with no resolvable sale order at all —
            # most commonly because Mercado Libre reported this order as
            # 'cancelled' before this connector ever created it (see
            # sale.order._meli_create_from_order_data's own recovery
            # logic, which this call re-enters with the order's current,
            # real data). _meli_import_order is idempotent and safe to
            # call even when it turns out there's nothing to recover
            # (e.g. a genuinely different, still-unresolvable status).
            #
            # Fix 2026-09-14 (real production incident: order
            # 2000018458354168, a permanent 404 on its shipments lookup
            # exhausted all 8 retries): this used to be called inline,
            # synchronously, wrapped in a try/except that deliberately
            # let RetryableJobError propagate so queue_job's own retry
            # machinery could see it. But this whole method runs inside
            # ONE job/transaction — when that exception propagated all
            # the way out (whether retried 8 times or failing
            # permanently), the enclosing savepoint in
            # queue_job_cron_jobrunner's _process() rolled back
            # EVERYTHING done during this job's run, including the
            # document create/write just above. A permanently-failing
            # recovery meant losing the invoice/credit-note document
            # entirely, not just failing to link it — confirmed exactly
            # this way in production.
            #
            # Enqueuing as its OWN job fixes this at the root: this job
            # now finishes (and commits the document) regardless of what
            # happens to the recovery attempt, which gets its own
            # separate transaction and its own 8 retries.
            # sale.order._meli_import_order already calls
            # meli.invoice.document._meli_relink_orphaned_documents right
            # after creating the order, so this document gets linked
            # automatically once (if) the recovery job succeeds.
            # identity_key dedupes: several documents for the same order
            # arriving close together must not enqueue redundant recovery
            # jobs.
            #
            # Fix 2026-09-24 (user-directed, real production noise: order
            # 2000000005969244 and several others in the same failed-job
            # review): an invoice can arrive via webhook mere seconds
            # before the order itself does through its own, entirely
            # separate pipeline — attempting the recovery immediately
            # just wastes an API call on a 404 that resolves itself
            # moments later anyway (every order is always imported
            # through its own path regardless of this one). eta=900 (15
            # minutes) gives that normal path every reasonable chance to
            # land first; identity_key still dedupes against it if the
            # order shows up before this job ever runs. priority=0 (was
            # 8): this connector is the only real consumer of this queue
            # ("invoicing-only build" — nothing else competes for it),
            # so once the delay elapses this should run immediately, not
            # queue behind other, truly lower-priority sweeps.
            self.env['sale.order'].sudo().with_delay(
                priority=0, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_recover_order_{order_id}",
                eta=900,
            )._meli_import_order(company_id, order_id)

        if document.sale_order_id:
            # savepoint + broad except, same convention used in
            # sale_order.py (_meli_auto_validate_full_pickings,
            # _meli_flag_status_change) and now in
            # stock_picking.py's _action_done() override: this document
            # row is the one durable record that Mercado Libre already
            # confirmed exists — a failure in the reconciler (unrelated
            # invoicing/CFDI trouble) must never roll back the
            # write/create above and lose it. Lower risk here than the
            # picking hook (both real callers of this method already
            # have their own failure isolation — queue_job's own
            # transaction boundary, or the batch-import wizard's own
            # try/except), but the same protection is cheap and keeps
            # the two hooks consistent.
            try:
                with self.env.cr.savepoint():
                    # Fix 2026-09-24 (user-directed, Monterrey XE2 total-
                    # cancellation project, Phase 1): a credit-note/
                    # devolution document arriving here is itself proof
                    # this order was cancelled on Mercado Libre's own
                    # side — see _meli_infer_cancelled_from_credit_note's
                    # own docstring for why meli_last_status can't
                    # already be trusted alone for a non-Full order.
                    if document.transaction_type in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES:
                        document.sale_order_id._meli_infer_cancelled_from_credit_note()
                    document.sale_order_id._meli_reconcile_invoicing()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: invoicing reconciliation "
                    "failed after document %s was upserted — left "
                    "pending for manual review.",
                    document.sale_order_id.client_order_ref, document.id,
                )
            # Fix 2026-09-14 (user request): "que cuadre factura con la
            # orden de venta" — meli_amount_mismatch is a stored compute
            # (pure, no side effects, always safe to recompute), but the
            # chatter warning itself is a one-off notification, guarded
            # by meli_amount_mismatch_notified so it never posts twice
            # for the same document even if this method runs again
            # later (e.g. a status-change notification for the same
            # order/document).
            if document.meli_amount_mismatch and not document.meli_amount_mismatch_notified:
                document.sale_order_id._meli_post_with_mention(_(
                    "El total facturado por Mercado Libre (%(xml_total)s) "
                    "no coincide con el total de esta venta en Odoo "
                    "(%(order_total)s) — revisar manualmente."
                ) % {
                    'xml_total': f"{document.meli_xml_total:.2f}",
                    'order_total': f"{document.sale_order_id.amount_total:.2f}",
                })
                document.meli_amount_mismatch_notified = True
        return document

    @staticmethod
    def _meli_parse_issue_date_from_xml(xml_bytes):
        """The CFDI's Fecha (root cfdi:Comprobante node) is stamped in
        Monterrey/CDMX local time by SAT rule, never UTC or with an
        offset — same fiscal timezone already used for the cancellation-
        window check in sale_order.py. Converted here to naive UTC for
        storage, since Odoo Datetime fields are always naive UTC
        internally. Returns False (never raises) on malformed XML or a
        missing/unparsable Fecha attribute — a document's XML should
        never fail to save just because this one extra field couldn't
        be derived.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError:
            return False
        fecha = root.get('Fecha')
        if not fecha:
            return False
        try:
            naive_local = datetime.fromisoformat(fecha)
        except ValueError:
            return False
        return (
            MELI_FISCAL_TIMEZONE.localize(naive_local)
            .astimezone(pytz.utc)
            .replace(tzinfo=None)
        )

    @staticmethod
    def _meli_parse_total_from_xml(xml_bytes):
        """The CFDI's own Total attribute (root cfdi:Comprobante node) —
        the real fiscal total Mercado Libre stamped, used by
        _compute_meli_amount_mismatch to catch a factura that doesn't
        match its sale order's own total (2026-09-14 user request:
        "que cuadre factura con la orden de venta"). Returns False
        (never raises) on malformed XML or a missing/unparsable Total,
        same convention as _meli_parse_issue_date_from_xml.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError:
            return False
        total = root.get('Total')
        if not total:
            return False
        try:
            return float(total)
        except ValueError:
            return False

    @staticmethod
    def _meli_parse_concepts_from_xml(xml_bytes):
        """Every cfdi:Concepto line item on this document's own CFDI —
        used to credit a partial-refund devolución against the EXACT
        product/quantity Mercado Libre reports, instead of guessing or
        reversing the whole source invoice (2026-09-16, user-directed:
        "aplicar la nota de crédito normalmente al producto que fue y
        con la cantidad exacta"). A CFDI Concepto never carries this
        connector's own SKU (confirmed against real production XML,
        2026-09-16 — only ClaveProdServ, a generic SAT catalog code, and
        Descripcion, the product's plain name) — callers match by
        Descripcion against a candidate line's own product name.

        Returns a list of {'descripcion': str, 'cantidad': float,
        'importe': float} dicts, oldest-in-document-order first. Empty
        list (never raises) on malformed XML or a document with no
        Conceptos — same "missing means not derivable" convention as
        _meli_parse_total_from_xml.
        """
        try:
            root = etree.fromstring(xml_bytes)
        except etree.XMLSyntaxError:
            return []
        concepts = []
        for node in root.iter():
            if etree.QName(node).localname != 'Concepto':
                continue
            descripcion = node.get('Descripcion')
            cantidad = node.get('Cantidad')
            importe = node.get('Importe')
            if not descripcion or cantidad is None or importe is None:
                continue
            try:
                concepts.append({
                    'descripcion': descripcion,
                    'cantidad': float(cantidad),
                    'importe': float(importe),
                })
            except ValueError:
                continue
        return concepts

    @staticmethod
    def _meli_extract_order_id(metadata):
        """Confirmed against a real 'invoices' notification (2026-09-04,
        a meli_resale/fiscaldocuments-v2 invoice): there is NO top-level
        order_id/resource_id field at all — the order id is
        items[0].external_order_id. Falls back to the previously-guessed
        field names in case a non-resale invoice's payload differs.
        """
        items = metadata.get('items') or []
        if items and items[0].get('external_order_id'):
            return str(items[0]['external_order_id'])
        return str(
            metadata.get('order_id')
            or metadata.get('resource_id')
            or (metadata.get('order') or {}).get('id')
            or ''
        )

    @staticmethod
    def _meli_extract_transaction_type(metadata):
        # Confirmed against the same real payload: fiscal_data.transaction_type.
        return (
            metadata.get('transaction_type')
            or (metadata.get('fiscal_data') or {}).get('transaction_type')
        )

    @api.model
    def _meli_import_invoice_document(self, company_id, invoice_id):
        """Entry point for the /meli/notifications webhook (topic
        'invoices'). Fetches metadata first to learn order_id/
        transaction_type, then prefers the order-based XML endpoint
        (the one _meli_import_invoice_document_for_order already uses,
        confirmed against the official docs) over fetching directly by
        invoice_id — a real 'invoices' notification (2026-09-04, a
        meli_resale invoice) proved that /invoice/$INVOICE_ID/xml can
        404 even for a real, existing, authorized invoice (its own
        metadata's xml_location field points at a completely different,
        "/internal/..." path with a UUID, not this invoice_id) — so
        fetching by invoice_id is now only the LAST-RESORT fallback,
        used when metadata didn't yield an order_id at all.
        """
        invoice_id = str(invoice_id)
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            _logger.info(
                "No connected Mercado Libre config found for company %s, "
                "skipping invoice document %s.", company_id, invoice_id,
            )
            return self.browse()

        metadata = {}
        try:
            metadata = config._api_get(
                f'/users/{config.ml_user_id}/invoices/{invoice_id}'
            ) or {}
        except requests.exceptions.RequestException:
            _logger.warning(
                "Could not fetch metadata for Mercado Libre invoice %s — "
                "falling back to fetching its XML directly by invoice_id.",
                invoice_id,
            )

        order_id = self._meli_extract_order_id(metadata)
        transaction_type = self._meli_extract_transaction_type(metadata)

        xml_bytes = None
        if order_id:
            try:
                xml_bytes = config._api_get_raw(
                    f'/invoices/io/documents/stream/order/{order_id}/xml',
                    params={'transaction_type': transaction_type} if transaction_type else None,
                )
            except requests.exceptions.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                _logger.warning(
                    "Mercado Libre invoice %s: order-based XML fetch "
                    "404'd for order %s/%s — falling back to invoice_id.",
                    invoice_id, order_id, transaction_type,
                )
        else:
            _logger.warning(
                "Mercado Libre invoice %s: could not determine its "
                "order_id from metadata %r — storing without a linked "
                "order.", invoice_id, metadata,
            )

        if xml_bytes is None:
            try:
                xml_bytes = config._api_get_raw(
                    f'/invoices/io/documents/stream/invoice/{invoice_id}/xml'
                )
            except requests.exceptions.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                _logger.warning(
                    "Mercado Libre invoice %s: could not fetch its XML by "
                    "any known endpoint (both order-based and invoice_id-"
                    "based fetches 404'd) — storing the record without a "
                    "file for manual follow-up.", invoice_id,
                )

        return self._meli_upsert(
            order_id or False, transaction_type, xml_bytes, meli_invoice_id=invoice_id,
            status=metadata.get('status'), company_id=company_id,
        )

    @api.model
    def _meli_import_invoice_document_for_batch_line(self, company_id, invoice_id, line_id, pack_id=False):
        """Entry point for queue_job when importing from the merged
        Invoice/Order/Pack Excel batch (see
        meli.invoice.import.batch.wizard). Wraps
        _meli_import_invoice_document but, unlike the webhook/
        missed_feeds callers, always records the outcome on the batch
        line instead of letting a failure surface only in the technical
        Queue Jobs view — the batch is meant to be the one place a
        person needs to check, so this never re-raises.

        `pack_id` (2026-09-14): when the same Excel row also carried a
        Pack ID, it can't be applied synchronously in the wizard for a
        NEW invoice_id — the document doesn't exist yet at that point,
        it's only created here, inside this background job. Applying it
        here instead keeps both pieces of the row's request (fetch the
        invoice, relate its pack) in the one place that actually knows
        once the document exists.
        """
        line = self.env['meli.invoice.import.batch.line'].sudo().browse(line_id)
        try:
            document = self.sudo()._meli_import_invoice_document(company_id, invoice_id)
        except RetryableJobError:
            # Fix 5 (2026-09-09, final review — Important): re-raised
            # unchanged, BEFORE the generic `except Exception` below — see
            # sale_order.py's _meli_import_order_for_batch_line for the
            # identical fix and full rationale (RetryableJobError is
            # itself an Exception subclass, so without this it would be
            # caught here and the batch line marked permanently 'error'
            # on the bulk-import path most likely to hit rate limiting).
            raise
        except Exception as exc:
            _logger.exception(
                "Mercado Libre invoice batch import: invoice %s failed.", invoice_id,
            )
            line.write({'status': 'error', 'message': str(exc)[:500]})
            return
        if not document or not document.xml_file:
            # Either there was no connected config, or Mercado Libre
            # never recognized this invoice_id (both metadata and XML
            # fetches 404'd) — most likely a mistyped ID. Distinct from
            # 'error' so the person knows there's nothing to retry
            # without first double-checking the id itself.
            line.write({'status': 'not_found'})
            return
        if pack_id:
            related = document._meli_assign_linked_pack_id(pack_id)
            line.write({
                'status': 'related' if related else 'still_orphan',
                'document_ids': [(6, 0, document.ids)],
            })
            return
        line.write({'status': 'imported', 'document_ids': [(6, 0, document.ids)]})

    @api.model
    def _meli_recover_invoices_in_range(self, company_id, date_from, date_to):
        """Entry point for the recovery wizard's single enqueued job.
        Moved here (2026-09-08) so the wizard's action_recover() never
        does any enumeration itself — a real date range can span
        thousands of orders (1,100+ orders/day), and even just looping
        to call with_delay() for each (order, transaction_type) pair
        inside the request made the wizard hang for minutes. Now that
        loop runs here, inside a background job, so the wizard only
        ever enqueues this one job and returns immediately.

        Re-resolves the connection at execution time (not passed in by
        the caller) since a job can run long after the button was
        clicked, by which point the connection could have been
        disconnected — same defensive pattern as
        _meli_import_invoice_document.
        """
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            _logger.info(
                "No connected Mercado Libre config found for company %s, "
                "skipping invoice recovery for range %s..%s.",
                company_id, date_from, date_to,
            )
            return 0

        orders = self.env['sale.order'].search([
            ('company_id', '=', config.company_id.id),
            ('partner_id', '=', config.partner_id.id),
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
        ])
        queued = 0
        for order in orders:
            # Known gap: for a Ventiapp-created order that was part of a
            # Mercado Libre pack, client_order_ref/reference hold the
            # PACK id, not this individual order's id (same convention
            # xe_meli_connector itself replicates for new orders — see
            # sale_order.py) — Mercado Libre's invoice endpoint wants the
            # order id specifically, so those orders won't match here.
            # meli_order_id (only set on xe_meli_connector's own
            # imports) is unambiguous and tried first.
            order_id = order.meli_order_id or order.client_order_ref or order.reference
            if not order_id:
                continue
            for transaction_type in MELI_INVOICE_TRANSACTION_TYPES:
                self.with_delay(
                    # Priority 6 (vs. the 8 used by order/claim polling
                    # and batch import) — invoices got de-prioritized by
                    # default and lagged behind, per the user 2026-09-04.
                    priority=6, channel='root.meli_sales', max_retries=8,
                    description=(
                        f"Import Mercado Libre invoice for order {order_id} "
                        f"({transaction_type}, recovery)"
                    ),
                    identity_key=f"meli_import_invoice_order_{order_id}_{transaction_type}",
                )._meli_import_invoice_document_for_order(
                    company_id, order_id, transaction_type,
                )
                queued += 1
        return queued

    @api.model
    def _meli_import_invoice_document_for_order(self, company_id, order_id, transaction_type):
        """Confirmed-safe path (unlike the webhook's metadata lookup
        above): order_id and transaction_type are already known by the
        caller (the date-range recovery wizard), so this only needs the
        one documented, verified endpoint. Returns an empty recordset
        (not an error) when Mercado Libre has no document of this
        transaction_type for this order (HTTP 404) — expected most of
        the time, since the caller tries every known transaction_type
        per order.
        """
        order_id = str(order_id)
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company_id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            return self.browse()
        try:
            xml_bytes = config._api_get_raw(
                f'/invoices/io/documents/stream/order/{order_id}/xml',
                params={'transaction_type': transaction_type},
            )
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return self.browse()
            raise
        return self._meli_upsert(order_id, transaction_type, xml_bytes, company_id=company_id)

    def action_meli_refresh_status(self):
        """Manual, on-demand pull of the current status straight from
        Mercado Libre — for the user to double-check a specific
        document without waiting for a new webhook notification.
        Synchronous: triggered by a person on a handful of selected
        records, not a batch job, so a couple of inline API calls is
        fine (unlike the recovery wizard, which enumerates whole date
        ranges and must never do that inside a request).

        Only works for documents that already know their
        meli_invoice_id — Mercado Libre never exposes it from order_id
        alone (confirmed against the real API, 2026-09-04), so a
        document recovered blindly by date range can't be refreshed
        this way; those are silently skipped and reported back.
        """
        updated, skipped = self._meli_refresh_status_now()
        message = _("%(updated)s documento(s) actualizado(s).") % {'updated': updated}
        if skipped:
            message += ' ' + _(
                "%(skipped)s omitido(s): Mercado Libre nunca da su Invoice "
                "ID (fueron rescatados por rango de fechas)."
            ) % {'skipped': skipped}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Mercado Libre"), 'message': message, 'type': 'success'},
        }

    def _meli_refresh_status_now(self):
        """Shared core for action_meli_refresh_status (manual button)
        and _cron_refresh_pending_authorization_documents (2026-09-20
        user-directed follow-up: a 'pending_authorization' or
        'interrupted' document — neither a final state — never changed
        its own status on its own; nothing but this manual button ever
        re-checked it, so it could sit that way forever unless someone
        happened to click it). Returns (updated_count, skipped_count).
        """
        refreshable = self.filtered(lambda document: document.meli_invoice_id)
        skipped = len(self) - len(refreshable)
        updated = 0
        for document in refreshable:
            config = self.env['meli.config'].sudo().search([
                ('company_id', '=', document.company_id.id), ('state', '=', 'connected'),
            ], limit=1)
            if not config:
                continue
            try:
                metadata = config._api_get(
                    f'/users/{config.ml_user_id}/invoices/{document.meli_invoice_id}'
                )
            except requests.exceptions.RequestException:
                continue
            status = (metadata or {}).get('status')
            if status:
                document.status = status
                updated += 1
                # Fix 2026-09-20 (user-directed, real production bug):
                # a plain field write here — unlike _meli_upsert, which
                # always re-triggers reconciliation after writing new
                # values — never made anything react to the new status.
                # A document that just turned out to be cancelled/
                # rejected on Mercado Libre's own side, for instance,
                # would sit here with its status correctly refreshed in
                # Odoo while the invoice built from it stayed confirmed
                # forever, since nothing ever re-checked it. Same
                # savepoint + broad except convention _meli_upsert
                # itself already uses for this exact call, so a failure
                # here never rolls back the status write above.
                if document.sale_order_id:
                    try:
                        with self.env.cr.savepoint():
                            document.sale_order_id._meli_reconcile_invoicing()
                    except Exception:
                        _logger.exception(
                            "Mercado Libre document %s: invoicing "
                            "reconciliation failed after a status "
                            "refresh — left pending for manual review.",
                            document.id,
                        )
        return updated, skipped

    def _cron_refresh_pending_authorization_documents(self):
        """Backup cron (2026-09-20 user-directed follow-up): a document
        stuck in 'pending_authorization' or 'interrupted' — neither a
        final state, see MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES's own
        docstring in sale_order.py — never changes on its own; before
        this, only a person clicking 'Actualizar Status' by hand ever
        re-checked it, so a document could sit that way forever
        otherwise (real production data: 98 pending_authorization + 6
        interrupted documents found, none of them ever re-checked).
        Reuses the exact same core the manual button itself uses, over
        every document currently in one of those two statuses — cheap
        and safe to run frequently: a document that's still not
        authorized yet just gets skipped (no status change) until it
        genuinely is. Deliberately scoped to ONLY these two statuses
        (2026-09-20 user decision) — re-checking every already-
        'authorized' document too (tens of thousands, growing by the
        thousands every two weeks in this account's own real data)
        would be a lot of unnecessary API traffic for a transition
        that's rare and, for the vast majority of documents, already
        covered by Mercado Libre's own 'invoices' webhook whenever it
        does fire.
        """
        documents = self.sudo().search([
            ('status', 'in', list(MELI_INVOICE_NOT_YET_AUTHORIZED_STATUSES)),
            ('meli_invoice_id', '!=', False),
        ])
        if documents:
            documents._meli_refresh_status_now()

    def _meli_rescue_xml_for_documents(self):
        """Shared core for action_meli_rescue_xml (manual button) and
        _cron_rescue_missing_xml_documents (backup) — re-fetches the XML
        file for every document in `self` that doesn't have one yet,
        through whichever endpoint its own known identifiers support:
        meli_invoice_id when known (an ordinary document whose XML fetch
        simply failed every time it was attempted — e.g. Mercado Libre's
        endpoint 404'd or was briefly down), falling back to
        meli_order_id + transaction_type otherwise (a document recovered
        by the date-range wizard, which never learns its own
        meli_invoice_id at all). Returns (rescued_count, still_missing_count).
        """
        Document = self.env['meli.invoice.document'].sudo()
        rescued = 0
        still_missing = 0
        for document in self:
            config = self.env['meli.config'].sudo().search([
                ('company_id', '=', document.company_id.id), ('state', '=', 'connected'),
            ], limit=1)
            if not config:
                still_missing += 1
                continue
            try:
                if document.meli_invoice_id:
                    Document._meli_import_invoice_document(
                        config.company_id.id, document.meli_invoice_id,
                    )
                elif document.meli_order_id and document.transaction_type:
                    Document._meli_import_invoice_document_for_order(
                        config.company_id.id, document.meli_order_id,
                        document.transaction_type,
                    )
                else:
                    still_missing += 1
                    continue
            except Exception:
                _logger.exception(
                    "Mercado Libre invoice document %s: XML rescue failed.",
                    document.id,
                )
                still_missing += 1
                continue
            document.invalidate_recordset(['xml_file'])
            if document.xml_file:
                rescued += 1
            else:
                still_missing += 1
        return rescued, still_missing

    def action_meli_rescue_xml(self):
        """Manual button: on-demand re-fetch of the XML file for
        selected documents missing one — 'Actualizar Status' only ever
        refreshes the metadata status, never retries the file itself,
        so a document stuck without its XML (every fetch attempt 404'd,
        or Mercado Libre's endpoint was briefly down) had no way to
        retry short of a full date-range recovery. Synchronous: a
        person triggers this on a handful of selected records.
        """
        rescuable = self.filtered(lambda document: not document.xml_file)
        skipped = len(self) - len(rescuable)
        rescued, still_missing = rescuable._meli_rescue_xml_for_documents()
        message = _("%(rescued)s XML rescatado(s).") % {'rescued': rescued}
        if still_missing:
            message += ' ' + _(
                "%(missing)s sin cambios (sin conexión activa o Mercado "
                "Libre sigue sin tenerlo)."
            ) % {'missing': still_missing}
        if skipped:
            message += ' ' + _("%(skipped)s ya tenían XML.") % {'skipped': skipped}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Mercado Libre"), 'message': message, 'type': 'success'},
        }

    def action_meli_retry_sale_order_confirmation(self):
        """Manual button (2026-09-14 user request): retries confirming
        this document's linked sale order when it's stuck in draft —
        the same action_confirm() sale.order._meli_create_from_order_
        data already tries automatically on creation. Covers any
        "escenario extraño" left it unconfirmed and hence never
        delivered/invoiced (a real production case, order S841238: "no
        se pudo serializar el acceso debido a un update concurrente" —
        that specific transient-DB-conflict case is now ALSO retried
        automatically at creation time, see _meli_create_from_order_
        data's own 2026-09-14 fix; this button remains the manual
        escape hatch for any other cause — a validation error since
        fixed, a missing SKU mapping added afterward, etc.).

        Multi-record, same UX convention as action_meli_rescue_xml:
        skips whatever doesn't qualify instead of failing the whole
        batch, and reports one summary notification.
        """
        confirmed = 0
        skipped = 0
        failed = []
        for document in self:
            order = document.sale_order_id
            if not order or order.state != 'draft':
                skipped += 1
                continue
            try:
                order.action_confirm()
            except Exception as exc:
                failed.append((order.client_order_ref, str(exc)))
                continue
            if order.state == 'sale':
                confirmed += 1
                config = self.env['meli.config'].sudo().search([
                    ('company_id', '=', order.company_id.id), ('state', '=', 'connected'),
                ], limit=1)
                is_fulfillment = bool(
                    config and config.warehouse_fulfillment_id
                    and order.warehouse_id == config.warehouse_fulfillment_id
                )
                order._meli_ensure_delivery(is_fulfillment)
                order.message_post(body=_(
                    "Sale confirmed manually via \"Retry Confirmation\" "
                    "on its Mercado Libre invoice document."
                ))
        message = _("%(confirmed)s venta(s) confirmada(s).") % {'confirmed': confirmed}
        if skipped:
            message += ' ' + _(
                "%(skipped)s omitida(s) (sin venta relacionada o ya no "
                "está en borrador)."
            ) % {'skipped': skipped}
        if failed:
            message += ' ' + _("%(failed)s siguen fallando: %(detail)s") % {
                'failed': len(failed),
                'detail': '; '.join(f"{ref}: {err}" for ref, err in failed[:3]),
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Mercado Libre"), 'message': message,
                'type': 'success' if confirmed and not failed else 'warning',
            },
        }

    def action_meli_repair_missing_product(self):
        """Manual server action (2026-09-18 user request), deliberately
        NOT exposed as a list/form button — meant to be wired up as an
        ir.actions.server the user creates themselves (Settings >
        Technical > Server Actions, bound to this model, "Execute
        Python Code": `action = records.action_meli_repair_missing_
        product()`), so it can be toggled on/off independently of a
        code deploy.

        For each selected document with a resolvable pack, reuses the
        exact same pack-discovery fix as automatic imports (see
        sale.order._meli_ensure_all_pack_siblings_imported's own
        docstring, Fix 2026-09-18, order S970525): fetches the pack's
        real order list from Mercado Libre and enqueues import of any
        sibling order_id Odoo doesn't have a line for yet. That enqueued
        job does everything on its own — adds the missing product line,
        and, if the order is Full, delivers it (_meli_ensure_delivery)
        — nothing else to do here.

        Existing (2026-09-14) meli_amount_mismatch records are the
        intended target: they were flagged before this pack-discovery
        fix existed, so nothing has rechecked them since. Asynchronous
        by design (with_delay, same identity_key convention as every
        other recovery path in this module) — the summary notification
        reports what was queued, not the outcome; the caller checks
        back on the sale order afterward.
        """
        queued = 0
        no_pack = 0
        skipped = 0
        for document in self:
            order = document.sale_order_id
            pack_id = document.meli_pack_id or (order.meli_pack_id if order else False)
            if not order or not pack_id:
                no_pack += 1
                continue
            config = self.env['meli.config'].sudo().search([
                ('company_id', '=', document.company_id.id), ('state', '=', 'connected'),
            ], limit=1)
            if not config:
                skipped += 1
                continue
            known_order_id = order.meli_order_id or document.meli_order_id
            order.sudo()._meli_ensure_all_pack_siblings_imported(config, pack_id, known_order_id)
            queued += 1
        message = _(
            "%(queued)s venta(s) puesta(s) en cola para revisión de "
            "producto faltante."
        ) % {'queued': queued}
        if no_pack:
            message += ' ' + _(
                "%(no_pack)s sin pack o sin venta relacionada (nada que "
                "hacer)."
            ) % {'no_pack': no_pack}
        if skipped:
            message += ' ' + _(
                "%(skipped)s omitida(s) (sin conexión configurada para "
                "esa compañía)."
            ) % {'skipped': skipped}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Mercado Libre"), 'message': message, 'type': 'success'},
        }

    @api.model
    def _cron_rescue_missing_xml_documents(self):
        """30-minute safety-net cron (xe_meli_connector/data/ir_cron.xml)
        equivalent to a person clicking 'Rescatar XML' on every document
        still missing its file — same "primary path + backup cron"
        pattern already used elsewhere in this module (see
        _cron_relink_orphaned_documents, _cron_retry_unapplied_documents).
        """
        documents = self.sudo().search([('meli_has_xml', '=', False)])
        documents._meli_rescue_xml_for_documents()
