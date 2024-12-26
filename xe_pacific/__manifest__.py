# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI Pacific Rim",
    "version": "17.0.0.0.3",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'base',
        'xe_customs',
        'sale_project',
        'helpdesk_fsm',
        'helpdesk_sale',
        'mrp',
        #'hr',
        'industry_fsm',
        'sale_management',
        'mail',
        'sale_stock',
    ],
    "category": "Accounting",
    "summary": "MX-EDI XE Brands",
    'data': [
        'data/ir_config_parameter.xml',
        'data/email_backorder_to_salesperson.xml',
        'data/email_notify_seller_of_changes.xml',
        'wizard/stock_backorder_confirmation.xml',
        'wizard/base_copy_user_access.xml',
        'wizard/cancellation_remision.xml',
        'views/fsm_project_menus.xml',
        'views/sale_views.xml',
        #'views/helpdesk_views.xml',
        'views/stock_picking_views.xml',
        'views/inventory_tag_views.xml',
        'views/inventory_tag_menu.xml',
        'views/product_template.xml',
        'views/res_partner_views.xml',
        'views/account_move_views.xml',
        'views/hr_employee_views.xml',
        'views/res_user_views.xml',
        'views/cancelled_remission_reason_views.xml',
        'views/cancelled_remission_views.xml',
        'views/analytic_distribution_model.xml',
        'security/ir.model.access.csv',
        'security/xe_pacific_security.xml'
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
    "images":['static/src/img/imagenVisitasTecnicas.png'],
}
