import base64
import io

import openpyxl

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..models.meli_config import MELI_BATCH_IMPORT_SECONDS_BETWEEN_JOBS


def _clean_cell(cell_value):
    if cell_value is None:
        return None
    if isinstance(cell_value, float) and cell_value.is_integer():
        return str(int(cell_value))
    return str(cell_value).strip()


def _meli_extract_invoice_batch_rows(file_content):
    """Reads the first sheet of an .xlsx file and returns a list of
    (raw_invoice, raw_order, raw_pack, invoice_id, order_id, pack_id)
    tuples, one per non-empty row, in file order. Column A is the
    Mercado Libre Invoice ID, column B its Order ID, column C its Pack
    ID — all three optional per row (at least one must parse to a
    valid id), so the same file can drive a straight API import
    (Invoice ID), a local-only relate-to-sale (Order ID + Pack ID with
    no Invoice ID), or both at once.

    A cell is treated as a valid Mercado Libre id only if it reduces to
    a string of digits: either already text, or a whole number
    (openpyxl reads an unformatted numeric cell as a float — current-
    format Mercado Libre ids are safely below float64's 2**53 exact-
    integer limit, so this doesn't lose precision for real ids).
    Anything else (a header, blank padding, stray text) comes back as
    None in that position so the caller can still show the raw value in
    the report instead of it disappearing silently.
    """
    workbook = openpyxl.load_workbook(
        io.BytesIO(file_content), read_only=True, data_only=True,
    )
    sheet = workbook.worksheets[0]
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if not row or all(cell is None for cell in row):
            continue
        raw_invoice = _clean_cell(row[0]) if len(row) > 0 else None
        raw_order = _clean_cell(row[1]) if len(row) > 1 else None
        raw_pack = _clean_cell(row[2]) if len(row) > 2 else None
        if raw_invoice is None and raw_order is None and raw_pack is None:
            continue
        invoice_id = raw_invoice if raw_invoice and raw_invoice.isdigit() else None
        order_id = raw_order if raw_order and raw_order.isdigit() else None
        pack_id = raw_pack if raw_pack and raw_pack.isdigit() else None
        rows.append((
            raw_invoice or '', raw_order or '', raw_pack or '',
            invoice_id, order_id, pack_id,
        ))
    return rows


class MeliInvoiceImportBatchWizard(models.TransientModel):
    _name = 'meli.invoice.import.batch.wizard'
    _description = 'Bulk-import/relate Mercado Libre Invoices from an Excel file (Invoice ID / Order ID / Pack ID)'

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
            rows = _meli_extract_invoice_batch_rows(base64.b64decode(self.excel_file))
        except Exception as exc:
            raise UserError(_(
                "Could not read %(filename)s as an Excel file: %(error)s"
            ) % {'filename': self.filename or '', 'error': str(exc)}) from exc

        batch = self.env['meli.invoice.import.batch'].sudo().create({
            'company_id': company.id,
        })
        Document = self.env['meli.invoice.document'].sudo()
        Line = self.env['meli.invoice.import.batch.line'].sudo()
        seen_keys = set()
        # Staggered, not fired all at once — a real, production self-
        # inflicted API-traffic burst (2026-09-10) when an Excel with
        # thousands of rows enqueued every job with the same eta. Only
        # rows that actually reach the API (a new Invoice ID) consume a
        # slot — an Order-ID-only row never calls with_delay() at all.
        enqueued_count = 0

        for raw_invoice, raw_order, raw_pack, invoice_id, order_id, pack_id in rows:
            common_vals = {
                'batch_id': batch.id, 'raw_value': raw_invoice,
                'raw_order_value': raw_order, 'raw_pack_value': raw_pack,
            }
            if invoice_id is None and order_id is None:
                # Neither an Invoice ID nor an Order ID to locate a
                # document by — a lone Pack ID has nothing to attach to.
                Line.create({**common_vals, 'pack_id': pack_id, 'status': 'invalid'})
                continue
            # Fix 2026-09-14 (real file, 12,809 rows): the same Invoice
            # ID legitimately repeats across several DIFFERENT orders —
            # one factura can cover more than one order/pack. Deduping
            # on Invoice ID alone (the previous key) silently dropped
            # every one of those legitimate extra orders as 'duplicate'.
            # Only an EXACT repeat of the full (invoice, order, pack)
            # combination is a real duplicate — anything else is a
            # distinct row that still needs its own line/relate attempt.
            key = (invoice_id, order_id, pack_id)
            if key in seen_keys:
                Line.create({
                    **common_vals,
                    'invoice_id': invoice_id, 'order_id': order_id, 'pack_id': pack_id,
                    'status': 'duplicate',
                    'message': _("Already listed earlier in this same file."),
                })
                continue
            seen_keys.add(key)

            if invoice_id:
                existing = Document.search([('meli_invoice_id', '=', invoice_id)], limit=1)
                if existing:
                    status = 'already_existed'
                    if pack_id:
                        related = existing._meli_assign_linked_pack_id(pack_id)
                        status = 'related' if related else 'still_orphan'
                    Line.create({
                        **common_vals,
                        'invoice_id': invoice_id, 'order_id': order_id, 'pack_id': pack_id,
                        'status': status, 'document_ids': [(6, 0, existing.ids)],
                    })
                    continue

                line = Line.create({
                    **common_vals,
                    'invoice_id': invoice_id, 'order_id': order_id, 'pack_id': pack_id,
                    'status': 'pending',
                })
                # Note: when this invoice_id already got its own row
                # earlier in this same file (different order/pack — see
                # the dedup fix above), the shared identity_key means
                # queue_job returns the FIRST row's still-pending job
                # instead of creating a new one for THIS line — so this
                # line stays 'pending' with its own order_id/pack_id
                # correctly recorded, not silently lost, until "Retry
                # Pending/Failed" is used once the invoice has actually
                # been imported (by then the identity_key is free again
                # and this line's own pack_id gets applied).
                Document.with_delay(
                    priority=8, channel='root.meli_sales', max_retries=8,
                    identity_key=f"meli_import_invoice_{invoice_id}",
                    eta=enqueued_count * MELI_BATCH_IMPORT_SECONDS_BETWEEN_JOBS,
                )._meli_import_invoice_document_for_batch_line(
                    company.id, invoice_id, line.id, pack_id=pack_id,
                )
                enqueued_count += 1
                continue

            # No Invoice ID: Order ID is the only way to locate an
            # already-known document — never calls the Mercado Libre API.
            documents = Document.search([('meli_order_id', '=', order_id)])
            if not documents:
                Line.create({
                    **common_vals, 'order_id': order_id, 'pack_id': pack_id,
                    'status': 'not_found',
                })
                continue
            if pack_id:
                related = any(
                    document._meli_assign_linked_pack_id(pack_id)
                    for document in documents
                )
                status = 'related' if related else 'still_orphan'
            else:
                status = 'already_existed'
            Line.create({
                **common_vals, 'order_id': order_id, 'pack_id': pack_id,
                'status': status, 'document_ids': [(6, 0, documents.ids)],
            })

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'meli.invoice.import.batch',
            'res_id': batch.id,
            'view_mode': 'form',
            'target': 'current',
        }
