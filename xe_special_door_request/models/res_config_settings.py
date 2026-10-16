from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Registros sin ID externo en producción: se eligen aquí (parámetros globales)
    special_door_sku_validator_id = fields.Many2one(
        'res.users',
        string="Validador de SKUs",
        config_parameter='xe_special_door_request.sku_validator_user_id',
        help="Usuario que recibe la actividad para validar los SKUs nuevos.",
    )
    special_door_tag_single_id = fields.Many2one(
        'product.tag',
        string="Etiqueta Sencilla",
        config_parameter='xe_special_door_request.tag_single_id',
    )
    special_door_tag_fixed_id = fields.Many2one(
        'product.tag',
        string="Etiqueta Con fijo",
        config_parameter='xe_special_door_request.tag_fixed_id',
    )
    special_door_tag_double_id = fields.Many2one(
        'product.tag',
        string="Etiqueta Doble",
        config_parameter='xe_special_door_request.tag_double_id',
    )
    special_door_sale_tag_id = fields.Many2one(
        'crm.tag',
        string="Etiqueta de la orden de venta",
        config_parameter='xe_special_door_request.sale_tag_id',
        help="Etiqueta que se agrega a la orden de venta al crear la solicitud.",
    )
