from odoo import models, fields, api


class ConnectSpreadsheetCredential(models.Model):
    _name = 'connect.spreadsheet.credential'
    _description = 'Connect Spreadsheet Credential'

    name = fields.Char('Name')
    service_account_json_credentials = fields.Text('Service Account JSON Credentials',
                                                   help='You can download it from:'
                                                        'https://console.cloud.google.com/apis/credentials?project=YourProject,'
                                                        'create or click on the current service account, go to the KEYS tab,'
                                                        'then click on the ADD KEY button, then Create new key,'
                                                        'and download it JSON, then paste the value insde the JSON file here')
    scopes = fields.Many2many('authorization.scope', string='Scopes')
