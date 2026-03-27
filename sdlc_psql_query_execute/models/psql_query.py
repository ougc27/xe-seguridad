import json
from odoo import fields, models
from odoo.exceptions import ValidationError

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class PsqlQuery(models.Model):
    _name = 'psql.query'
    _description = 'PostgreSQL Query'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Query Name', required=True, tracking=True)
    query = fields.Text(string='Query')
    result = fields.Html(string='Result', readonly=True)
    column_names = fields.Text(string='Column Names', readonly=True)
    row_data = fields.Text(string='Row Data', readonly=True)

    def action_execute_query(self):
        """Execute the SQL query and display results as an HTML table."""
        for rec in self:
            if not rec.query:
                raise ValidationError("Please enter a SQL query to execute.")
            query = rec.query.strip()
            # Block non-SELECT queries for safety
            if not query.upper().startswith('SELECT'):
                raise ValidationError(
                    "Only SELECT queries are allowed for safety reasons."
                )
            try:
                self.env.cr.execute(query)
                columns = [desc[0] for desc in self.env.cr.description]
                rows = self.env.cr.fetchall()
            except Exception as e:
                raise ValidationError(
                    "Error executing SQL query: %s" % str(e)
                )
            # Build HTML table
            html = ('<table class="table table-bordered table-hover '
                    'table-sm o_psql_result_table" '
                    'style="width: max-content; min-width: 100%;">')
            # Header
            html += '<thead><tr>'
            for col in columns:
                html += ('<th style="background-color: #87CEEB; '
                         'padding: 8px; text-align: center; '
                         'white-space: nowrap;">'
                         '%s</th>' % col)
            html += '</tr></thead>'
            # Body
            html += '<tbody>'
            for row in rows:
                html += '<tr>'
                for cell in row:
                    html += ('<td style="padding: 6px; text-align: center; '
                             'white-space: nowrap;">'
                             '%s</td>' % (
                                 cell if cell is not None else 'None'))
                html += '</tr>'
            html += '</tbody></table>'
            rec.result = html
            # Store data for XLSX export
            rec.column_names = json.dumps(columns)
            rec.row_data = json.dumps(
                [[str(cell) if cell is not None else 'None' for cell in row]
                 for row in rows]
            )

    def action_download_xlsx(self):
        """Generate and download XLSX report of query results."""
        self.ensure_one()
        if not self.column_names or not self.row_data:
            raise ValidationError(
                "No results to export. Please execute a query first."
            )
        return {
            'type': 'ir.actions.act_url',
            'url': '/psql_query/download_xlsx/%s' % self.id,
            'target': 'new',
        }
