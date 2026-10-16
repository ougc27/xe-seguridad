from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Marca placeholders (servicio) y SKUs especiales (almacenables)
    is_special_door = fields.Boolean(
        string="Puerta de medida especial",
        tracking=True,
        help="Placeholders y SKUs de puertas de medida especial. "
             "Las líneas de la orden de venta con estos productos habilitan la solicitud.",
    )
