from odoo import fields, models


class SpecialDoorColor(models.Model):
    _name = 'special.door.color'
    _description = "Color de puerta de medida especial"
    _order = 'name'

    name = fields.Char(string="Nombre", required=True)
    code = fields.Char(string="Código", required=True, help="Clave del color en el SKU.")
    active = fields.Boolean(string="Activo", default=True)

    _sql_constraints = [
        ('code_unique', 'unique(code)', "Ya existe un color con ese código."),
    ]
