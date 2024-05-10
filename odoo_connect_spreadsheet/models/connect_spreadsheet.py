import pytz
import json
import ast
import datetime


from odoo.addons.odoo_connect_spreadsheet.models import _helpers
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class Partner(models.Model):
    _inherit = "res.partner"

    """We can read more detail about the selection value bellow from here:
    https://developers.google.com/drive/api/v2/reference/permissions/insert#request-body"""
    grant_type = fields.Selection([('user', 'User')], default='user', required=True)  # todo | set as owner selection
    grant_role = fields.Selection([('writer', 'Writer'), ('reader', 'Reader')], default='reader', required=True)


class ConnectSpreadsheet(models.Model):
    _name = 'connect.spreadsheet'
    _description = 'Connect Spreadsheet'
    _inherit = ['mail.thread']
    _rec_name = 'spreadsheet_title'

    credential_id = fields.Many2one('connect.spreadsheet.credential', string='Credential', required=True)
    pure_sql = fields.Boolean('Pure SQL')
    search_query = fields.Text('Search Query (*Only select)')
    preview_query_result = fields.Html('Preview Query Result')
    model_id = fields.Many2one('ir.model', string='Model', ondelete='cascade', required=True,
                               help='This must be always required even with pure sql set to true, '
                                    'one of the purposes is for realtime updates. If pure sql is set to true, '
                                    'consider the realtime update, realtime update depends on the selected model '
                                    'on this field.')
    model_name = fields.Char(related='model_id.model', string='Model Name', store=True)
    spreadsheet_field_ids = fields.One2many('connect.spreadsheet.fields', 'connect_spreadsheet_id', string='Fields',
                                            copy=True)
    state = fields.Selection([('draft', 'Draft'), ('active', 'Active')],
                             default='draft')  # todo prevent delete if active
    update_type = fields.Selection([('realtime', 'Realtime'), ('cron_job', 'Cron Job'), ('manual', 'Manual')],
                                   required=True)
    existing_or_new = fields.Selection([('existing', 'Existing'), ('create_new', 'Create New')], required=True)
    spreadsheet_id = fields.Char('Spreadsheet ID')
    spreadsheet_url = fields.Char('Spreadsheet URL')
    spreadsheet_title = fields.Char('Spreadsheet Title')
    sheet_name = fields.Char('Sheet Name', default='Sheet1', required=True)
    range_name = fields.Char('Range Name', required=True, default='A1')
    clear_sheet = fields.Boolean('Clear Sheet?', default=False,
                                 help='Mark as True to clear all data inside the Sheet Name.')
    partner_ids = fields.Many2many('res.partner', string='Grant sheet access')
    show_sync_spreadsheet_button = fields.Boolean('Show the Update Spreadsheet button?', default=False)
    show_header = fields.Boolean('Show header?', default=True,
                                 help='The header is the title of the row, which is obtained from '
                                      'the name of the selected field')
    order_with = fields.Many2one('ir.model.fields', string='Order with', domain="[('model_id', '=', model_id)]")
    order_type = fields.Selection([('asc', 'ASCENDING'), ('desc', 'DESCENDING')], default='desc')
    limit = fields.Integer('Limit')
    domain = fields.Char(string="Filter")
    enable_domain_filter = fields.Boolean('Enable Domain Filter')
    operation_type = fields.Selection([('update', 'Update'), ('append', 'Append')], default='update', required=True,
                                      help='In the Google Sheets API, "update" refers to the process of modifying existing data in a sheet, while "append" refers to the process of adding new data to a sheet.')
    insert_data_option = fields.Selection([('OVERWRITE', 'OVERWRITE'), ('INSERT_ROWS', 'INSERT_ROWS')],
                                          default='INSERT_ROWS', required=True,
                                          help="Determines how existing data is changed when new data is input.\n"
                                               "OVERWRITE: The new data overwrites existing data in the areas it is written. (Note: adding data to the end of the sheet will still insert new rows or columns so the data can be written.).\n"
                                               "INSERT_ROWS: Rows are inserted for the new data.")
    major_dimension = fields.Selection([('ROWS', 'ROWS'), ('COLUMNS', 'COLUMNS')], default='ROWS', required=True,
                                       help="Indicates which dimension an operation should apply to.\n"
                                            "ROWS: Operates on the rows of a sheet.\n"
                                            "COLUMNS: Operates on the columns of a sheet.")

    @api.model
    def _get_default_timezone(self):
        return self.env.user.tz

    timezone = fields.Selection(selection='_get_timezone_list', default=_get_default_timezone, required=True)

    @api.model
    def _get_timezone_list(self):
        all_timezones = []
        for tz in pytz.all_timezones:
            all_timezones.append((tz, tz))
        return all_timezones

    @api.model
    def create(self, vals):
        # Make the range_name field uppercase before writing it to the database
        vals['range_name'] = vals['range_name'].upper()
        return super().create(vals)

    def write(self, vals):
        # Make the range_name field uppercase before writing it to the database
        if 'range_name' in vals:
            vals['range_name'] = vals['range_name'].upper()
        return super().write(vals)

    def scope(self):
        SCOPES = []
        for i in self.credential_id.scopes:
            SCOPES.append(i.scope)
        return SCOPES

    def drive_service(self):
        return _helpers.google_drive_authentication(self.scope(), json.loads(
            self.credential_id.service_account_json_credentials))

    def spreadsheet_service(self):
        return _helpers.google_sheet_authentication(self.scope(), json.loads(
            self.credential_id.service_account_json_credentials))

    def update_spreadsheet_permission(self):
        for partner in self.partner_ids:
            if partner.email and partner.grant_type and partner.grant_role:
                _helpers.update_spreadsheet_permission(self.drive_service(), self.spreadsheet_id,
                                                       partner.grant_type, partner.grant_role, partner.email)

    def _operation_type(self, values):
        """Update the data in the selected sheet and range"""
        if self.operation_type == 'update':
            _helpers.update_spreadsheet(self.spreadsheet_service(), self.spreadsheet_id,
                                        self.sheet_name + '!' + self.range_name if self.sheet_name else
                                        self.range_name, values)
        else:
            _helpers.append_values(self.spreadsheet_service(), self.spreadsheet_id,
                                   self.sheet_name + '!' + self.range_name if self.sheet_name else
                                   self.range_name, values, self.insert_data_option, self.major_dimension)

    def sync_spreadsheet(self):
        """Add a new sheet if that doesn't exist"""
        sheets_name = []
        for sheet in _helpers.spreadsheet_metadata(self.spreadsheet_service(), self.spreadsheet_id)['sheets']:
            sheets_name.append(sheet["properties"]["title"])
        if self.sheet_name not in sheets_name:
            _helpers.add_sheet(self.spreadsheet_service(), self.spreadsheet_id, self.sheet_name)

        if self.clear_sheet is True:
            # todo | clear all sheet (make sheet be blank), or clear in specify range
            """
            If any changes detect on range_name field, clear all data inside the sheet, and refill/update it 
            with bellow update data code. Ths logic is related with _onchange_range_name method.

            Set the sheet and the range of cells to clear, for this purpose, clear all cells.
            """
            _helpers.clear_spreadsheet_data(self.spreadsheet_service(), self.spreadsheet_id,
                                            self.sheet_name + '!A1:Z1000' if self.sheet_name else "A1:Z1000")
            self.clear_sheet = False

        values = []
        if self.pure_sql:
            self.test_query()
            for rec in self.execute_query():
                values.append(rec)
            """Update the data in the selected sheet and range"""
            self._operation_type(values)
        else:
            header_title = []
            body_tmp = []
            set_timezone = pytz.timezone(self.timezone)
            order = str(self.order_with.name) + " " + self.order_type if self.order_with else None
            """Set the title header on the spreadsheet from the selected field"""
            # TODO | If with header should we make the header string be bold?
            if self.show_header:
                for field in self.spreadsheet_field_ids:
                    header_title.append(field.field_id.field_description)
                values.append(header_title)

            """Grab rows of data based on the selected field"""
            try:
                model = self.env[self.model_id.model].search(ast.literal_eval(self.domain), order=order,
                                                             limit=self.limit)
            except ValueError:
                # Handle the error when the domain is empty
                model = self.env[self.model_id.model].search([], order=order, limit=self.limit)

            for field in self.spreadsheet_field_ids:
                for m in model:
                    if m.mapped(field.field_id.name):
                        value = m.mapped(field.field_id.name)
                        if value is not None and value != 0:
                            if field.field_id.relation:
                                value = value.mapped(field.display_field.name)
                            elif field.field_id.ttype == 'datetime' and self.timezone:
                                if isinstance(value, list):
                                    # Exclude boolean values from the list before applying astimezone
                                    value = [item for item in value if isinstance(item, datetime.datetime)]
                                    # Apply astimezone to each item in the list
                                    value = [item.astimezone(set_timezone) for item in value]
                                else:
                                    # Handle the case where value is a single datetime
                                    value = value.astimezone(set_timezone)

                            # Extract the actual value from the list and handle special cases
                            if isinstance(value, list):
                                # Handle lists
                                if all(isinstance(item, int) for item in value):
                                    # Join integers with commas
                                    value = ', '.join(map(str, value))
                                else:
                                    # Convert other lists to strings
                                    value = ', '.join(map(str, value))
                            else:
                                # Convert other types to strings
                                value = str(value)

                            body_tmp.append(value)
                        else:
                            body_tmp.append(False)
                    else:
                        body_tmp.append(False)

            n = len(model)
            endlist = [[] for _ in range(n)]
            for index, item in enumerate(body_tmp):
                endlist[index % n].append(item)
            values.extend(endlist)
            """Update the data in the selected sheet and range"""
            self._operation_type(values)

        if self.update_type != 'manual':
            """After spreadsheet updated, set the show_sync_spreadsheet_button to False again"""
            self.show_sync_spreadsheet_button = False

    def execute_query(self):
        if self.pure_sql and self.search_query:
            if not (self.search_query.lower().startswith('select') or self.search_query.lower().startswith('with')):
                raise UserError(_('Only SELECT and WITH queries are allowed'))

            result = []
            try:
                if self.show_header:
                    """Retrieve header name"""
                    cr = self.env.cr
                    cr.execute(self.search_query)
                    header_names = [row for row in self.env.cr.dictfetchall()[0]]
                    result.append(header_names)

                """Retrieve query result"""
                cr = self.env.cr
                cr.execute(self.search_query)
                for res in cr.fetchall():
                    fetch = list(map(str, res))
                    result.append(list(fetch))
            except Exception as e:
                _logger.error(e)
                raise UserError(_(e))

            return result

    def test_query(self):
        try:
            data = self.execute_query()
            table = '<div class="table-responsive"> <table class="table table-striped table-hover">'
            # Add table headers
            table += '<thead><tr>'
            for header in data[0]:
                table += f'<th>{header}</th>'
            table += '</tr></thead>'

            # Add table body
            table += '<tbody>'
            start_index = max(0, len(data) - 10)  # Calculate the starting index
            for row in data[start_index:]:  # Iterate through the last 10 rows
                table += '<tr>'
                for col in row:
                    table += f'<td>{col}</td>'
                table += '</tr>'
            table += '</tbody></table></div>'

            self.preview_query_result = table
        except Exception as e:
            raise UserError(_(e))

    def run(self):
        self.state = 'active'

        """Call the _onchange_update_type_and_state method to update the show_sync_spreadsheet_button field value"""
        self._onchange_update_type_and_state()

        if self.existing_or_new == 'create_new':
            self.spreadsheet_id = _helpers.create_spreadsheet(self.spreadsheet_service(),
                                                              self.spreadsheet_title if self.spreadsheet_title else None)
            self.spreadsheet_url = 'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0'.format(
                spreadsheet_id=self.spreadsheet_id)

            self.update_spreadsheet_permission()
            self.sync_spreadsheet()

        if self.existing_or_new == 'existing':
            self.spreadsheet_url = 'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid=0'.format(
                spreadsheet_id=self.spreadsheet_id)
            self.spreadsheet_title = _helpers.spreadsheet_metadata(self.spreadsheet_service(),
                                                                   self.spreadsheet_id)['properties']['title']
            try:
                self.sync_spreadsheet()
            except Exception as e:
                raise ValidationError(_(e))

    def reset_to_draft(self):
        self.state = 'draft'

    def sync_spreadsheet_every_x_times(self):
        try:
            for spreadsheet in self.env['connect.spreadsheet'].search(
                    [('state', '=', 'active'), ('update_type', '=', 'cron_job')]):
                spreadsheet.sync_spreadsheet()
        except Exception as e:
            _logger.error(e)

    @api.onchange('existing_or_new')
    def _onchange_existing_or_new(self):
        # todo | clear the spreadsheet_title if detected any changes from "Create New" to "Existing"

        if self.existing_or_new == 'create_new':
            self.spreadsheet_id = False

        if self.existing_or_new == 'existing':
            res = {'warning': {
                'title': _('Warning'),
                'message': _('If you will use an existing spreadsheet, please do not forget to '
                             'give %s permissions to the existing spreadsheet' %
                             json.loads(self.credential_id.service_account_json_credentials).get('client_email'))
            }}
            if res:
                return res

    @api.onchange('update_type', 'state')
    def _onchange_update_type_and_state(self):
        if self.state == 'active' and self.update_type == 'manual':
            self.show_sync_spreadsheet_button = True
        else:
            self.show_sync_spreadsheet_button = False

    @api.onchange('spreadsheet_field_ids', 'sheet_name', 'range_name', 'clear_sheet')
    def _onchange_fields_to_sync(self):
        self.show_sync_spreadsheet_button = True

    @api.onchange('pure_sql')
    def _onchange_pure_sql(self):
        if not self.pure_sql:
            self.preview_query_result = False
        if self.pure_sql:
            self.enable_domain_filter = False

    @api.onchange('enable_domain_filter')
    @api.constrains('enable_domain_filter')
    def _check_enable_domain_filter(self):
        for rec in self:
            if rec.enable_domain_filter and not rec.model_id:
                raise ValidationError(_('Please select a model first before enabling the domain filter!'))


class ConnectSpreadsheetFields(models.Model):
    _name = 'connect.spreadsheet.fields'
    _description = 'Connect Spreadsheet Fields'

    connect_spreadsheet_id = fields.Many2one('connect.spreadsheet')
    model_id = fields.Many2one(related='connect_spreadsheet_id.model_id')
    field_id = fields.Many2one('ir.model.fields', string='Field', domain="[('model_id', '=', model_id)]")
    model_relation = fields.Many2one('ir.model', compute='_compute_model_relation', store=True)
    display_field = fields.Many2one('ir.model.fields', domain="[('model_id', '=', model_relation)]")
    set_display_field_readonly = fields.Boolean('Set display_field as readonly?', default=False)

    @api.depends('field_id')
    def _compute_model_relation(self):
        for rec in self:
            if rec.field_id.relation:
                rec.model_relation = self.env['ir.model'].search([('model', '=', rec.field_id.relation)])

    @api.onchange('field_id')
    def _onchange_field_id(self):
        if self.field_id.ttype not in ['one2many', 'many2many', 'many2one']:
            self.display_field = self.field_id
            self.set_display_field_readonly = True
        else:
            self.set_display_field_readonly = False

    @api.constrains('display_field')
    def _constrains_display_field(self):
        for rec in self:
            if not rec.display_field:
                raise UserError(_('Please fill the Display Field relation value'))
