import base64
import io

import openpyxl

from odoo import _, fields, models
from odoo.exceptions import UserError


def _meli_extract_invoice_ids(file_content):
    """Reads the first sheet of an .xlsx file and returns a list of
    (raw_value, invoice_id_or_None) tuples, one per non-empty row, in
    file order. Only the first column of each row is read — this is a
    single-column import, not a general spreadsheet parser.

    A cell is treated as a valid Mercado Libre invoice ID only if it
    reduces to a string of digits: either already text, or a whole
    number (openpyxl reads an unformatted numeric cell as a float —
    current-format Mercado Libre invoice IDs are safely below float64's
    2**53 exact-integer limit, so this doesn't lose precision for real
    invoice IDs). Anything else (a header, blank padding at the end of
    the sheet, stray text) comes back as (raw_value, None) so the
    caller can still show it in the report instead of it disappearing
    silently. Same parsing rules as _meli_extract_order_ids in
    meli_import_batch_wizard.py, kept as its own copy here since the
    two imports report on different columns (invoice_id vs order_id).
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
        invoice_id = raw_value if raw_value.isdigit() else None
        rows.append((raw_value, invoice_id))
    return rows


class MeliInvoiceImportBatchWizard(models.TransientModel):
    _name = 'meli.invoice.import.batch.wizard'
    _description = 'Bulk-import Mercado Libre Invoices from an Excel file, by Invoice ID'

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
            rows = _meli_extract_invoice_ids(base64.b64decode(self.excel_file))
        except Exception as exc:
            raise UserError(_(
                "Could not read %(filename)s as an Excel file: %(error)s"
            ) % {'filename': self.filename or '', 'error': str(exc)}) from exc

        batch = self.env['meli.invoice.import.batch'].sudo().create({
            'company_id': company.id,
        })
        Document = self.env['meli.invoice.document'].sudo()
        seen_invoice_ids = set()
        for raw_value, invoice_id in rows:
            if invoice_id is None:
                self.env['meli.invoice.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'status': 'invalid',
                })
                continue
            if invoice_id in seen_invoice_ids:
                self.env['meli.invoice.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'invoice_id': invoice_id, 'status': 'duplicate',
                    'message': _("Already listed earlier in this same file."),
                })
                continue
            seen_invoice_ids.add(invoice_id)

            existing = Document.search([('meli_invoice_id', '=', invoice_id)], limit=1)
            if existing:
                self.env['meli.invoice.import.batch.line'].sudo().create({
                    'batch_id': batch.id, 'raw_value': raw_value,
                    'invoice_id': invoice_id, 'status': 'already_existed',
                    'document_id': existing.id,
                })
                continue

            line = self.env['meli.invoice.import.batch.line'].sudo().create({
                'batch_id': batch.id, 'raw_value': raw_value,
                'invoice_id': invoice_id, 'status': 'pending',
            })
            Document.with_delay(
                priority=8, channel='root.meli_sales', max_retries=8,
                identity_key=f"meli_import_invoice_{invoice_id}",
            )._meli_import_invoice_document_for_batch_line(company.id, invoice_id, line.id)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'meli.invoice.import.batch',
            'res_id': batch.id,
            'view_mode': 'form',
            'target': 'current',
        }
