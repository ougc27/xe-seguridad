import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class MeliImportBatch(models.Model):
    _name = 'meli.import.batch'
    _description = 'Mercado Libre Bulk Order Import (from Excel)'
    _order = 'create_date desc'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    line_ids = fields.One2many(
        'meli.import.batch.line', 'batch_id', string='Lines',
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
            batch.error_count = statuses.count('error') + statuses.count('not_paid')

    def action_retry_pending(self):
        """Re-enqueues every line that isn't in a final state yet — for
        when the connection was down, or a missing SKU mapping just got
        completed, and the person doesn't want to re-upload the same
        Excel file. Uses the exact same channel/identity_key convention
        as the webhook/polling/single-order wizard (see
        sale.order._meli_import_order_for_batch_line), so a job already
        in flight for the same Mercado Libre order is never duplicated.
        """
        SaleOrder = self.env['sale.order']
        for batch in self:
            retryable = batch.line_ids.filtered(
                lambda line: line.status in ('pending', 'not_paid', 'error')
            )
            for line in retryable:
                line.status = 'pending'
                SaleOrder.with_delay(
                    priority=8, channel='root.meli_sales',
                    identity_key=f"meli_import_order_{line.order_id}",
                )._meli_import_order_for_batch_line(
                    batch.company_id.id, line.order_id, line.id,
                )


class MeliImportBatchLine(models.Model):
    _name = 'meli.import.batch.line'
    _description = 'Mercado Libre Bulk Order Import Line'
    _order = 'id'

    batch_id = fields.Many2one(
        'meli.import.batch', string='Batch', required=True, ondelete='cascade',
    )
    raw_value = fields.Char(
        string='Excel Value',
        help="The raw value read from the Excel row, as-is — kept even "
             "for rows that couldn't be parsed as a Mercado Libre order "
             "ID, so nothing from the file disappears silently.",
    )
    order_id = fields.Char(string='Mercado Libre Order ID')
    status = fields.Selection([
        ('pending', 'Pending'),
        ('imported', 'Imported'),
        ('already_existed', 'Already Existed'),
        ('not_paid', 'Not Paid Yet'),
        ('duplicate', 'Duplicate in File'),
        ('invalid', 'Invalid Row'),
        ('error', 'Error'),
    ], string='Status', required=True, default='pending')
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', readonly=True)
    message = fields.Char(string='Detail', readonly=True)
