from odoo import models, fields, api


class AuthorizationScope(models.Model):
    _name = 'authorization.scope'
    _description = 'Authorization Scope'
    _rec_name = 'scope'

    scope = fields.Char('Scope')
