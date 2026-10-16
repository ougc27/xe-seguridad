# See LICENSE file for full copyright and licensing details.

{
    "name": "Solicitud de puertas de medida especial",
    "version": "17.0.1.0.1",
    "license": "LGPL-3",
    "author": "XE Brands",
    "category": "Sales/Sales",
    "summary": "Solicitud de puertas de medida especial desde la orden de venta, "
               "con tarea de seguimiento y alta automática de SKUs por validar",
    "depends": [
        'sale_project',
        'sale_stock',
        'purchase_stock',
        'product_unspsc',
        'mail',
    ],
    "data": [
        'security/ir.model.access.csv',
        'data/project_data.xml',
        'data/product_data.xml',
        'views/special_door_catalog_views.xml',
        'views/product_template_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/special_door_request_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
