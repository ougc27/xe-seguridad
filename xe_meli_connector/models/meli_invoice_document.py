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
MELI_INVOICE_DEAD_STATUSES = {'rejected', 'cancelled'}


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
    issue_date = fields.Datetime(
        string='Fecha de Emisión',
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
             "retry (Sale Order's own 'Retry Invoicing Reconciliation' "
             "button).",
    )
    is_applied = fields.Boolean(
        string='Applied in Odoo', compute='_compute_is_applied', store=True,
        help="True once move_ids is non-empty — see that field's own "
             "help text for what 'applied' means here.",
    )

    @api.depends('move_ids')
    def _compute_is_applied(self):
        for document in self:
            document.is_applied = bool(document.move_ids)

    _sql_constraints = [(
        'invoice_id_uniq', 'unique(meli_invoice_id)',
        'This Mercado Libre invoice is already registered.',
    )]

    @api.depends('meli_order_id')
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
                    document.sale_order_id._meli_reconcile_invoicing()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: invoicing reconciliation "
                    "failed after a previously-orphaned document %s was "
                    "relinked — left pending for manual review.",
                    document.sale_order_id.client_order_ref, document.id,
                )

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
            # Deliberately NOT wrapped in its own cr.savepoint() (final
            # whole-branch review, 2026-09-09, Critical C1): recovering a
            # Full order can reach sale.order._meli_recover_cancelled_on_
            # arrival_full, which commits the cursor directly between its
            # two phases (same commit-before-reconciling rule
            # _meli_flag_status_change already follows). An enclosing
            # savepoint here made its own RELEASE fail right after that
            # inner commit, empirically proven — silently discarding a
            # fully successful recovery and aborting the transaction.
            # _meli_import_order_for_batch_line calls the same method
            # with no enclosing savepoint of its own, for the same reason
            # — matched here.
            try:
                self.env['sale.order'].sudo()._meli_import_order(company_id, order_id)
            except RetryableJobError:
                # Must propagate unchanged (final whole-branch review,
                # Important I1): a transient network failure inside the
                # recovery attempt (e.g. _meli_fetch_logistic_type or
                # config._api_get hitting a 429/5xx) must let queue_job's
                # own retry machinery see it, not get silently swallowed
                # here and logged as a permanent failure. Same fix
                # already applied twice elsewhere in this module today
                # (sale_order.py's _meli_import_order_for_batch_line,
                # this file's own _meli_import_invoice_document_for_batch_line).
                raise
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: could not recover a missing "
                    "sale order after document %s arrived — left "
                    "pending for manual review.", order_id, document.id,
                )
            else:
                document.invalidate_recordset(['sale_order_id'])

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
                    document.sale_order_id._meli_reconcile_invoicing()
            except Exception:
                _logger.exception(
                    "Mercado Libre order %s: invoicing reconciliation "
                    "failed after document %s was upserted — left "
                    "pending for manual review.",
                    document.sale_order_id.client_order_ref, document.id,
                )
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
    def _meli_import_invoice_document_for_batch_line(self, company_id, invoice_id, line_id):
        """Entry point for queue_job when importing from the Invoice-ID
        Excel batch (see meli.invoice.import.batch.wizard). Wraps
        _meli_import_invoice_document but, unlike the webhook/
        missed_feeds callers, always records the outcome on the batch
        line instead of letting a failure surface only in the technical
        Queue Jobs view — the batch is meant to be the one place a
        person needs to check, so this never re-raises.
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
        line.write({'status': 'imported', 'document_id': document.id})

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
