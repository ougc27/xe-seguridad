import io
import json
from odoo import http
from odoo.http import request, content_disposition

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class PsqlQueryController(http.Controller):

    @http.route('/psql_query/download_xlsx/<int:record_id>',
                type='http', auth='user')
    def download_xlsx(self, record_id, **kwargs):
        """Download query results as XLSX file."""
        record = request.env['psql.query'].browse(record_id)
        if not record.exists() or not record.column_names:
            return request.not_found()

        columns = json.loads(record.column_names)
        rows = json.loads(record.row_data)
        company_name = request.env.company.name

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Sheet1')

        # Styles
        title_style = workbook.add_format({
            'bold': True, 'font_size': 12, 'align': 'center',
        })
        header_style = workbook.add_format({
            'bold': True, 'font_size': 10, 'align': 'center',
            'bg_color': '#87CEEB', 'border': 1,
        })
        cell_style = workbook.add_format({
            'font_size': 10, 'align': 'center', 'border': 1,
        })
        cell_style_alt = workbook.add_format({
            'font_size': 10, 'align': 'center', 'border': 1,
            'bg_color': '#E8D0F0',
        })

        # Report date and company
        from datetime import date
        sheet.merge_range(0, 0, 0, 1,
                          'Report Date: %s' % date.today().isoformat(),
                          title_style)
        sheet.write(1, 0, company_name, title_style)

        # Column headers at row 4
        for col_idx, col_name in enumerate(columns):
            sheet.write(4, col_idx, col_name, header_style)
            sheet.set_column(col_idx, col_idx, max(len(col_name) + 4, 15))

        # Data rows starting at row 5
        for row_idx, row in enumerate(rows):
            style = cell_style_alt if row_idx % 2 == 0 else cell_style
            for col_idx, cell in enumerate(row):
                sheet.write(5 + row_idx, col_idx, cell, style)

        workbook.close()
        output.seek(0)
        xlsx_data = output.read()

        filename = '%s.xlsx' % (record.name or 'query_result')
        return request.make_response(
            xlsx_data,
            headers=[
                ('Content-Type',
                 'application/vnd.openxmlformats-officedocument'
                 '.spreadsheetml.sheet'),
                ('Content-Disposition', content_disposition(filename)),
            ],
        )
