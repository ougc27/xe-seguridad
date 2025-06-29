from odoo import fields, models


class HebStore(models.Model):
    _name = 'heb.store'
    _description = 'HEB Store'

    name = fields.Char(required=True)
    branch_number = fields.Integer(required=True)
    gln = fields.Char(required=True)
