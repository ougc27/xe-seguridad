from odoo import fields, models


class SpecialDoorModel(models.Model):
    _name = 'special.door.model'
    _description = "Modelo de puerta de medida especial"
    _order = 'name'

    name = fields.Char(string="Nombre", required=True)
    code = fields.Char(string="Código", required=True, help="Prefijo del SKU.")
    categ_id = fields.Many2one(
        'product.category',
        string="Categoría de producto",
        help="Categoría con la que se crean los SKUs nuevos de este modelo.",
    )
    product_tag_ids = fields.Many2many(
        'product.tag',
        'special_door_model_product_tag_rel',
        'model_id',
        'tag_id',
        string="Serie",
        help="Etiquetas de serie que se asignan a los SKUs nuevos.",
    )
    single_max_width = fields.Integer(
        string="Ancho máximo sencilla (cm)",
        help="Ancho máximo, inclusive, para etiquetar la puerta como Sencilla. "
             "Vacío: se usa el valor general (119 cm).",
    )
    active = fields.Boolean(string="Activo", default=True)

    _sql_constraints = [
        ('code_unique', 'unique(code)', "Ya existe un modelo con ese código."),
    ]
