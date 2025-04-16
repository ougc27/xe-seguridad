from odoo import fields, models


class HelpdeskAttribution(models.Model):
    _name = 'helpdesk.attribution'
    _description = 'Helpdesk Attribution'

    name = fields.Char(required=True)
    _sql_constraints = [
        ('name_uniq', 'unique (name)', "A type with the same name already exists."),
    ]
