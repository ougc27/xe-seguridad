import base64
import io

import openpyxl

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.meli_config import MELI_BATCH_IMPORT_SECONDS_BETWEEN_JOBS


def _meli_extract_order_ids(file_content):
    """Reads the first sheet of an .xlsx file and returns a list of
    (raw_value, order_id_or_None) tuples, one per non-empty row, in
    file order. Only the first column of each row is read — this is a
    single-column import, not a general spreadsheet parser.

    A cell is treated as a valid Mercado Libre order ID only if it
    reduces to a string of digits: either already text, or a whole
    number (openpyxl reads an unformatted numeric cell as a float —
    current-format Mercado Libre order IDs, ~16 digits, are safely
    below float64's 2**53 exact-integer limit, so this doesn't lose
    precision for real order IDs). Anything else (a header, blank
    padding at the end of the sheet, stray text) comes back as
    (raw_value, None) so the caller can still show it in the report
    instead of it disappearing silently.
    """
    workbook = openpyxl.load_workbook(
        io.BytesIO(file_content), read_only=True, data_only=True,
    )
    sheet = workbook.worksheets[0]
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if not row or all(cell is None for cell in row):
            continue
        cell_value = row[0]
        if cell_value is None:
            continue
        if isinstance(cell_value, float) and cell_value.is_integer():
            raw_value = str(int(cell_value))
        else:
            raw_value = str(cell_value).strip()
        order_id = raw_value if raw_value.isdigit() else None
        rows.append((raw_value, order_id))
    return rows


class MeliImportBatchWizard(models.TransientModel):
    _name = 'meli.import.batch.wizard'
    _description = 'Bulk-import Mercado Libre Orders from an Excel file'

    excel_file = fields.Binary(string='Excel File', required=True)
    filename = fields.Char(string='Filename')

    def action_import(self):
        self.ensure_one()
        company = self.env.company
        config = self.env['meli.config'].sudo().search([
            ('company_id', '=', company.id), ('state', '=', 'connected'),
        ], limit=1)
        if not config:
            raise UserError(_(
                "There is no active Mercado Libre connection for this company."
            ))

        try:
            rows = _meli_extract_order_ids(base64.b64decode(self.excel_file))
        except Exception as exc:
            raise UserError(_(
                "Could not read %(filename)s as an Excel file: %(error)s"
            ) % {'filename': self.filename or '', 'error': str(exc)}) from exc

        batch = self.env['meli.import.batch'].sudo().create({
            'company_id': company.id,
        })
        SaleOrder = self.env['sale.order'].sudo()
        seen_order_ids = set()
        # Staggered, not fired all at once: an Excel with thousands of
        # rows used to enqueue every job with the same eta (now), which
        # — combined with a since-fixed queue_job_cron_jobrunner bug
        # that ignored each job's own retry_pattern — self-inflicted a
        # burst of API traffic against Mercado Libre (confirmed in
        # production, 2026-09-10). Only rows that actually get enqueued
        # count toward the stagger; invalid/duplicate/already-existing
        # rows never call with_delay() at all, so skipping them here
        # doesn't waste any of the pacing budget.
        enqueued_count = 0
        for raw_value, order_id in rows:
            if order_id is None:
                self.env['meli.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'status': 'invalid',
                })
                continue
            if order_id in seen_order_ids:
                self.env['meli.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'order_id': order_id, 'status': 'duplicate',
                    'message': _("Already listed earlier in this same file."),
                })
                continue
            seen_order_ids.add(order_id)

            existing = SaleOrder.search([('meli_order_id', '=', order_id)], limit=1)
            if existing:
                self.env['meli.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'order_id': order_id, 'status': 'already_existed',
                    'sale_order_id': existing.id,
                })
                continue

            line = self.env['meli.import.batch.line'].sudo().create({
                'batch_id': batch.id, 'raw_value': raw_value,
                'order_id': order_id, 'status': 'pending',
            })
            SaleOrder.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_import_order_{order_id}",
                eta=enqueued_count * MELI_BATCH_IMPORT_SECONDS_BETWEEN_JOBS,
            )._meli_import_order_for_batch_line(company.id, order_id, line.id)
            enqueued_count += 1

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'meli.import.batch',
            'res_id': batch.id,
            'view_mode': 'form',
            'target': 'current',
        }
