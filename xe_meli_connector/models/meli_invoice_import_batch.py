import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MeliInvoiceImportBatch(models.Model):
    _name = 'meli.invoice.import.batch'
    _description = 'Mercado Libre Bulk Invoice Import (from Excel: Invoice ID / Order ID / Pack ID)'
    _order = 'create_date desc'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    line_ids = fields.One2many(
        'meli.invoice.import.batch.line', 'batch_id', string='Lines',
    )
    total_count = fields.Integer(compute='_compute_counts')
    pending_count = fields.Integer(compute='_compute_counts')
    imported_count = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')

    @api.depends('line_ids.status')
    def _compute_counts(self):
        for batch in self:
            statuses = batch.line_ids.mapped('status')
            batch.total_count = len(statuses)
            batch.pending_count = statuses.count('pending')
            batch.imported_count = (
                statuses.count('imported') + statuses.count('already_existed')
                + statuses.count('related') + statuses.count('still_orphan')
            )
            batch.error_count = statuses.count('error') + statuses.count('not_found')

    def action_retry_pending(self):
        """Re-enqueues every line that isn't in a final state yet AND
        actually has an Invoice ID to fetch (the only column that ever
        drives a real API call/queue_job — an Order-ID-only row that
        came back 'not_found' has nothing to retry against the API, its
        document simply doesn't exist in Odoo yet). For when the
        connection was down, or the invoice_id was mistyped and got
        fixed, and the person doesn't want to re-upload the same Excel
        file. Uses the exact same channel/identity_key convention as the
        webhook and missed_feeds reconciliation (see
        meli.invoice.document._meli_import_invoice_document_for_batch_line),
        so a job already in flight for the same invoice_id is never
        duplicated.
        """
        Document = self.env['meli.invoice.document']
        for batch in self:
            retryable = batch.line_ids.filtered(
                lambda line: line.status in ('pending', 'not_found', 'error')
                and line.invoice_id
            )
            for line in retryable:
                line.status = 'pending'
                Document.with_delay(
                    priority=8, channel='root.meli_sales', max_retries=8,
                    identity_key=f"meli_import_invoice_{line.invoice_id}",
                )._meli_import_invoice_document_for_batch_line(
                    batch.company_id.id, line.invoice_id, line.id,
                    pack_id=line.pack_id,
                )


class MeliInvoiceImportBatchLine(models.Model):
    _name = 'meli.invoice.import.batch.line'
    _description = 'Mercado Libre Bulk Invoice Import Line'
    _order = 'id'

    batch_id = fields.Many2one(
        'meli.invoice.import.batch', string='Batch', required=True, ondelete='cascade',
    )
    raw_value = fields.Char(
        string='Invoice ID (Excel)',
        help="The raw value read from the Excel row's Invoice ID column, "
             "as-is — kept even for rows that couldn't be parsed, so "
             "nothing from the file disappears silently.",
    )
    raw_order_value = fields.Char(string='Order ID (Excel)')
    raw_pack_value = fields.Char(string='Pack ID (Excel)')
    invoice_id = fields.Char(string='Mercado Libre Invoice ID')
    order_id = fields.Char(string='Mercado Libre Order ID')
    pack_id = fields.Char(string='Mercado Libre Pack ID')
    status = fields.Selection([
        ('pending', 'Pending'),
        ('imported', 'Imported'),
        ('already_existed', 'Already Existed'),
        ('related', 'Related to Sale'),
        ('still_orphan', 'Pack Saved, Still Orphan'),
        ('duplicate', 'Duplicate in File'),
        ('invalid', 'Invalid Row'),
        ('not_found', 'Not Found'),
        ('error', 'Error'),
    ], string='Status', required=True, default='pending')
    document_ids = fields.Many2many(
        'meli.invoice.document', string='Invoice Documents', readonly=True,
        help="Every document this row touched — usually one, but an "
             "Order ID shared by a factura and its nota de crédito "
             "updates both.",
    )
    message = fields.Char(string='Detail', readonly=True)
