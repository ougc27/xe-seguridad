import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MeliInvoiceImportBatch(models.Model):
    _name = 'meli.invoice.import.batch'
    _description = 'Mercado Libre Bulk Invoice Import (from Excel, by Invoice ID)'
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
            )
            batch.error_count = statuses.count('error') + statuses.count('not_found')

    def action_retry_pending(self):
        """Re-enqueues every line that isn't in a final state yet — for
        when the connection was down, or the invoice_id was mistyped and
        got fixed, and the person doesn't want to re-upload the same
        Excel file. Uses the exact same channel/identity_key convention
        as the webhook and missed_feeds reconciliation (see
        meli.invoice.document._meli_import_invoice_document_for_batch_line),
        so a job already in flight for the same invoice_id is never
        duplicated.
        """
        Document = self.env['meli.invoice.document']
        for batch in self:
            retryable = batch.line_ids.filtered(
                lambda line: line.status in ('pending', 'not_found', 'error')
            )
            for line in retryable:
                line.status = 'pending'
                Document.with_delay(
                    priority=8, channel='root.meli_sales',
                    identity_key=f"meli_import_invoice_{line.invoice_id}",
                )._meli_import_invoice_document_for_batch_line(
                    batch.company_id.id, line.invoice_id, line.id,
                )


class MeliInvoiceImportBatchLine(models.Model):
    _name = 'meli.invoice.import.batch.line'
    _description = 'Mercado Libre Bulk Invoice Import Line'
    _order = 'id'

    batch_id = fields.Many2one(
        'meli.invoice.import.batch', string='Batch', required=True, ondelete='cascade',
    )
    raw_value = fields.Char(
        string='Excel Value',
        help="The raw value read from the Excel row, as-is — kept even "
             "for rows that couldn't be parsed as a Mercado Libre invoice "
             "ID, so nothing from the file disappears silently.",
    )
    invoice_id = fields.Char(string='Mercado Libre Invoice ID')
    status = fields.Selection([
        ('pending', 'Pending'),
        ('imported', 'Imported'),
        ('already_existed', 'Already Existed'),
        ('duplicate', 'Duplicate in File'),
        ('invalid', 'Invalid Row'),
        ('not_found', 'Not Found on Mercado Libre'),
        ('error', 'Error'),
    ], string='Status', required=True, default='pending')
    document_id = fields.Many2one(
        'meli.invoice.document', string='Invoice Document', readonly=True,
    )
    message = fields.Char(string='Detail', readonly=True)
