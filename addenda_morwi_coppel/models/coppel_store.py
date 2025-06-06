from odoo import fields, models


class CoppelStore(models.Model):
    _name = 'coppel.store'
    _description = 'Coppel Store'

    coppel_branch_number = fields.Char(required=True)
    name = fields.Char('Branch Name', required=True)
