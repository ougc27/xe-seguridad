import base64
import logging
from datetime import datetime

from odoo import _, api, fields, models

import pytz
import requests
from lxml import etree

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
             "xe_meli_connector), falling back to client_order_ref/"
             "reference (today's Ventiapp-created orders, which don't "
             "populate meli_order_id at all), and finally to meli_pack_id "
             "— covers a resale/pack invoice whose own metadata reports "
             "the pack's id rather than one specific sibling order's id.",
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

    _sql_constraints = [(
        'invoice_id_uniq', 'unique(meli_invoice_id)',
        'This Mercado Libre invoice is already registered.',
    )]

    @api.depends('meli_order_id')
    def _compute_sale_order_id(self):
        SaleOrder = self.env['sale.order']
        for document in self:
            order = SaleOrder.browse()
            if document.meli_order_id:
                order = SaleOrder.search(
                    [('meli_order_id', '=', document.meli_order_id)], limit=1,
                )
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

    @staticmethod
    def _meli_document_type_for(transaction_type):
        return (
            'nota_de_credito'
            if transaction_type in MELI_INVOICE_CREDIT_NOTE_TRANSACTION_TYPES
            else 'factura'
        )

    def _meli_upsert(self, order_id, transaction_type, xml_bytes, meli_invoice_id=False, status=False):
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
            return existing
        return self.sudo().create(vals)

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
            status=metadata.get('status'),
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
                    priority=6, channel='root.meli_sales',
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
        return self._meli_upsert(order_id, transaction_type, xml_bytes)

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
